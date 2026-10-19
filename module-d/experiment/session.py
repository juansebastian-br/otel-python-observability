"""Run one guarded Module D session from Cloud Shell; never modifies shared apps."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiment import analyze, runner

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
NS = "observability-chaos-d"
LABELS = {"managed-by": "otel-module-d", "app.kubernetes.io/part-of": "module-d"}
PORTS = {"module-d-payments": (18001, 8001), "module-d-data": (18002, 8002),
         "module-d-prometheus": (19090, 9090), "module-d-alertmanager": (19093, 9093),
         "module-d-alert-receiver": (18080, 8080), "module-d-jaeger": (16686, 16686)}
CONTROL = "/var/run/chaos/control.json"
SAFE_LOG_FIELDS = {"timestamp", "observed_unix_ns", "event", "severity", "service_name",
                   "namespace", "route", "method", "mode", "delay_ms", "error_rate",
                   "trace_id", "span_id", "experiment_id"}


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def command(args, *, payload=None, timeout=25):
    result = subprocess.run(args, input=payload, text=True, capture_output=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("bounded command failed: " + args[0])
    return result.stdout


def owned(metadata, *, name=None, namespaced=True):
    if (not isinstance(metadata, dict) or any(metadata.get("labels", {}).get(k) != v for k, v in LABELS.items())
            or (name is not None and metadata.get("name") != name)
            or (namespaced and metadata.get("namespace") != NS)):
        raise ValueError("resource identity or Module D ownership was not verified")


def select_pod(deployment, replica_sets, pods):
    """Require one ready replica owned through the selected Deployment's RS."""
    meta, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
    name = meta.get("name")
    if name not in PORTS:
        raise ValueError("deployment is outside the explicit Module D allowlist")
    owned(meta, name=name)
    owned({**spec["template"]["metadata"], "namespace": NS})
    if (spec.get("replicas", 1) != 1 or status.get("readyReplicas") != 1
            or status.get("updatedReplicas") != 1 or status.get("replicas") != 1
            or status.get("observedGeneration", 0) < meta.get("generation", 1)):
        raise ValueError("deployment is not a stable single ready replica")
    containers = spec["template"]["spec"]["containers"]
    if len(containers) != 1 or containers[0]["name"] != name:
        raise ValueError("unreviewed application container layout")
    if PORTS[name][1] not in [p["containerPort"] for p in containers[0].get("ports", [])]:
        raise ValueError("unexpected container port")
    rs_uids = {r["metadata"]["uid"] for r in replica_sets if
               r["metadata"].get("namespace") == NS and any(o.get("kind") == "Deployment"
               and o.get("uid") == meta["uid"] for o in r["metadata"].get("ownerReferences", []))}
    selected = []
    for pod in pods:
        m, s = pod["metadata"], pod.get("status", {})
        if not any(o.get("kind") == "ReplicaSet" and o.get("uid") in rs_uids for o in m.get("ownerReferences", [])):
            continue
        owned(m)
        if (m.get("deletionTimestamp") or s.get("phase") != "Running"
                or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in s.get("conditions", []))):
            continue
        cs = s.get("containerStatuses", [])
        if len(cs) != 1 or cs[0].get("name") != name or cs[0].get("ready") is not True:
            continue
        selected.append({"name": m["name"], "uid": m["uid"], "container": name,
                         "deployment": name, "deployment_uid": meta["uid"],
                         "restart_count": cs[0].get("restartCount", 0)})
    if len(selected) != 1:
        raise ValueError("one unambiguous ready owned pod is required")
    return selected[0]


class Kubernetes:
    def __init__(self, project, location, cluster):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project):
            raise ValueError("invalid explicit project")
        if command(["gcloud", "config", "get-value", "project"]).strip() != project:
            raise ValueError("active gcloud project differs from --project")
        self.context = f"gke_{project}_{location}_{cluster}"
        if command(["kubectl", "config", "current-context"]).strip() != self.context:
            raise ValueError("kubectl context differs from the declared GKE cluster")
        namespace = json.loads(command(["kubectl", "--context", self.context, "get", "namespace", NS, "-o", "json"]))
        owned(namespace["metadata"], name=NS, namespaced=False)

    def args(self, args):
        return ["kubectl", "--context", self.context, "--namespace", NS] + args

    def call(self, args, **kwargs):
        return command(self.args(args), **kwargs)

    def pod(self, name):
        deployment = json.loads(self.call(["get", "deployment", name, "-o", "json"]))
        replicas = json.loads(self.call(["get", "replicasets", "-o", "json"]))["items"]
        pods = json.loads(self.call(["get", "pods", "-o", "json"]))["items"]
        return select_pod(deployment, replicas, pods)

    def control(self, selected, enabled, experiment_id, mode):
        if selected["deployment"] not in ("module-d-payments", "module-d-data"):
            raise ValueError("chaos control is restricted to the two app deployments")
        if enabled and selected["deployment"] != ("module-d-payments" if mode == "latency" else "module-d-data"):
            raise ValueError("fault mode does not match the target service")
        current = self.pod(selected["deployment"])
        if current != selected:
            raise ValueError("pod identity, owner or restart count changed during this experiment")
        script = control_script(enabled, experiment_id, mode)
        raw = self.call(["exec", selected["name"], "-c", selected["container"], "--", "python", "-c", script])
        result = json.loads(raw)
        if result.get("enabled") is not enabled:
            raise ValueError("control acknowledgement does not match the requested state")
        if enabled and not 0 < result["expires_at"] - result["issued_at"] <= 180:
            raise ValueError("control acknowledgement has an invalid TTL")
        return result

    def worker_image(self, selected):
        if selected["deployment"] != "module-d-payments" or self.pod("module-d-payments") != selected:
            raise ValueError("approved Python image source changed")
        deployment = json.loads(self.call(["get", "deployment", "module-d-payments", "-o", "json"]))
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("load worker requires the already approved immutable app image")
        return image

    def require_no_active_workers(self):
        jobs = json.loads(self.call(["get", "jobs", "-l", "module-d-component=load-generator", "-o", "json"]))
        for job in jobs.get("items", []):
            owned(job["metadata"])
            if not any(c.get("type") == "Complete" and c.get("status") == "True" for c in job.get("status", {}).get("conditions", [])):
                raise ValueError("another non-completed load worker exists; inspect it before a new experiment")


def control_script(enabled, experiment_id, mode):
    if not runner.SAFE_ID.fullmatch(experiment_id) or mode not in ("latency", "error"):
        raise ValueError("invalid control identity")
    # Issued/expiry use the target pod clock, never workstation assumptions.
    control = {"enabled": bool(enabled)}
    if enabled:
        control.update(experiment_id=experiment_id, mode=mode,
                       delay_ms=200 if mode == "latency" else 0, error_rate=0 if mode == "latency" else 0.10)
    return ("import json,os,time\n"
            f"control=json.loads({json.dumps(json.dumps(control))})\n"
            "if control['enabled']:\n"
            " control['issued_at']=time.time();control['expires_at']=control['issued_at']+180\n"
            f"path={CONTROL!r}\n"
            "temp=path+'.'+str(os.getpid())+'.tmp'\n"
            "with open(temp,'x',encoding='utf-8') as output:\n"
            " json.dump(control,output,allow_nan=False);output.flush();os.fsync(output.fileno())\n"
            "os.replace(temp,path)\n"
            "print(json.dumps(control,allow_nan=False))\n")


def fetch(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(8 * 1024 * 1024 + 1)
        if response.status != 200 or len(body) > 8 * 1024 * 1024:
            raise ValueError("bounded local monitoring response was not successful")
        return json.loads(body)
    finally:
        connection.close()


def verify_targets(value):
    targets = value.get("data", {}).get("activeTargets", [])
    wanted = {"payments", "data"}
    observed = {t.get("labels", {}).get("sandbox_role") for t in targets if
                t.get("labels", {}).get("job") == "module-d-apps" and t.get("health") == "up"}
    if len(targets) != 2 or observed != wanted:
        raise ValueError("both isolated app scrape targets must be UP")


class Forwards:
    def __init__(self, client, pods, out):
        self.client, self.pods, self.out = client, pods, out
        self.processes, self.files = [], []

    def start(self):
        # Fail if occupied; never adopt an existing unknown tunnel.
        reservations = []
        try:
            for local, _ in PORTS.values():
                sock = socket.socket()
                reservations.append(sock)
                sock.bind(("127.0.0.1", local))
        finally:
            for sock in reservations:
                sock.close()
        for name, (local, remote) in PORTS.items():
            output = (self.out / (name + "-port-forward.log")).open("x", encoding="utf-8")
            self.files.append(output)
            proc = subprocess.Popen(self.client.args(["port-forward", "pod/" + self.pods[name]["name"],
                "--address=127.0.0.1", f"{local}:{remote}"]), stdout=output, stderr=subprocess.STDOUT)
            self.processes.append(proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.healthy()
            ready = True
            for local, _ in PORTS.values():
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=0.3):
                        pass
                except OSError:
                    ready = False
            if ready:
                return
            time.sleep(0.3)
        raise TimeoutError("owned port-forwards did not become ready")

    def healthy(self):
        if len(self.processes) != len(PORTS) or any(p.poll() is not None for p in self.processes):
            raise RuntimeError("an owned port-forward exited")

    def close(self):
        failures = []
        for proc in self.processes:
            try:
                stop_process(proc)
            except Exception as exc:
                failures.append(type(exc).__name__)
        for output in self.files:
            output.close()
        if failures:
            raise RuntimeError("some owned port-forward processes could not be stopped")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


class InClusterWorker:
    """No Kubernetes credentials enter the worker; only this controller calls APIs."""
    def __init__(self, client, experiment_id, mode, image, out):
        from experiment import worker_resource
        self.resources = worker_resource
        self.client, self.experiment_id, self.mode, self.out = client, experiment_id, mode, out
        self.pod = None
        self.created, self.evidence_copied = [], False
        role, route, method, port = (("payments", "/payments", "POST", 8001) if mode == "latency"
                                     else ("data", "/data/health", "GET", 8002))
        host = f"module-d-{role}.{NS}.svc"
        config = {"experiment_id": experiment_id, "mode": mode, "target_url": f"http://{host}:{port}",
                  "path": route, "method": method,
                  "body": {"order_id": str(uuid.uuid4()), "amount": 100, "currency": "COP"} if role == "payments" else {}}
        sources = {name: (ROOT / "experiment" / name).read_text(encoding="utf-8")
                   for name in ("__init__.py", "runner.py", "analyze.py", "worker.py")}
        self.manifest = worker_resource.build_worker_manifest(experiment_id, image, sources, config)

    def create(self):
        self.client.require_no_active_workers()
        save(self.out / "worker-manifest.json", self.manifest)
        for item in self.manifest["items"]:
            owned(item["metadata"])
            kind, name = item["kind"].lower(), item["metadata"]["name"]
            prior = self.client.call(["get", kind, name, "--ignore-not-found", "-o", "json"])
            if prior.strip():
                raise ValueError("refusing to adopt a pre-existing worker resource")
            value = json.loads(self.client.call(["create", "-f", "-", "-o", "json"], payload=json.dumps(item)))
            owned(value["metadata"], name=name)
            self.created.append({"kind": kind, "name": name, "uid": value["metadata"]["uid"]})
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            job = json.loads(self.client.call(["get", "job", self.experiment_id, "-o", "json"]))
            pods = json.loads(self.client.call(["get", "pods", "-l", "job-name=" + self.experiment_id, "-o", "json"]))["items"]
            try:
                self.pod = self.resources.validate_ready_worker(job, pods)
                save(self.out / "worker-identity.json", self.pod)
                return
            except ValueError:
                if job.get("status", {}).get("failed"):
                    raise RuntimeError("load worker failed before becoming ready")
                time.sleep(2)
        raise TimeoutError("load worker startup exceeded 300 seconds")

    def call(self, script):
        if not self.pod:
            raise ValueError("load worker identity has not been verified")
        actual = json.loads(self.client.call(["get", "pod", self.pod["pod"], "-o", "json"]))
        owned(actual["metadata"], name=self.pod["pod"])
        if actual["metadata"]["uid"] != self.pod["pod_uid"]:
            raise ValueError("load worker pod UID changed")
        return self.client.call(["exec", self.pod["pod"], "-c", self.pod["container"], "--", "python", "-c", script], timeout=25)

    def read_state(self):
        raw = self.call("from pathlib import Path\np=Path('/evidence/phase-state.json')\nprint(p.read_text() if p.exists() else '{}')")
        state = json.loads(raw)
        if state and state.get("experiment_id") != self.experiment_id:
            raise ValueError("load worker state belongs to another experiment")
        return state

    def signal(self, phase=None, *, stop=False, collected=False):
        if sum((phase is not None, stop, collected)) != 1 or (phase is not None and phase not in analyze.PHASES):
            raise ValueError("invalid worker signal")
        filename = "stop" if stop else "collected" if collected else "release-" + phase + ".json"
        value = {"experiment_id": self.experiment_id, "phase": phase, "action": "continue"} if phase else {"experiment_id": self.experiment_id}
        script = ("import json,os\n"
                  f"p='/evidence/{filename}';v=json.loads({json.dumps(json.dumps(value))})\n"
                  "t=p+'.controller.tmp'\n"
                  "with open(t,'w',encoding='utf-8') as f:json.dump(v,f);f.flush();os.fsync(f.fileno())\n"
                  "os.replace(t,p)\n")
        self.call(script)

    def copy_events(self, stream, *, final=False):
        raw = self.call("from pathlib import Path\np=Path('/evidence/events.jsonl')\nprint(p.read_text() if p.exists() else '',end='')")
        # A concurrent append may leave the last line incomplete; retain only
        # newline-terminated rows until the final waiting/finished checkpoint.
        lines = raw.splitlines(keepends=True)
        complete = [line for line in lines if line.endswith("\n")]
        if final and len(complete) != len(lines):
            raise ValueError("final worker JSONL ends with an incomplete record")
        records = [json.loads(line) for line in complete]
        if any(r.get("experiment_id") != self.experiment_id for r in records):
            raise ValueError("mixed experiment IDs in worker evidence")
        stream.records = records
        path = self.out / "requests.jsonl"
        temp = path.with_suffix(".jsonl.pending")
        temp.write_text("".join(complete), encoding="utf-8")
        temp.replace(path)
        if final:
            self.evidence_copied = True

    def run(self, transition, stream):
        released, last_copy, last_state = [], 0.0, None
        deadline = time.monotonic() + 1650
        while time.monotonic() < deadline:
            state = self.read_state()
            if state != last_state:
                save(self.out / "worker-state.json", state)
                print(json.dumps({"event": "worker_state", "state": state}), flush=True)
                last_state = state
            if state.get("state") == "waiting":
                phase = state.get("phase")
                if phase not in released:
                    if phase != analyze.PHASES[len(released)]:
                        raise ValueError("out-of-order worker phase barrier")
                    self.copy_events(stream)
                    details = transition(phase)
                    save(self.out / ("controller-transition-" + phase + ".json"),
                         {"observed_at": runner.utc_now(), "phase": phase, "details": details})
                    self.signal(phase)
                    released.append(phase)
            if state.get("state") == "finished":
                self.copy_events(stream, final=True)
                if isinstance(state.get("summary"), dict):
                    save(self.out / "worker-summary.json", state["summary"])
                ends = [r for r in stream.records if r.get("record_type") == "run_end"]
                if len(ends) != 1:
                    raise ValueError("finished worker lacks exactly one run_end")
                return ends[0]
            if time.monotonic() - last_copy >= 30:
                self.copy_events(stream)
                last_copy = time.monotonic()
            time.sleep(2)
        raise TimeoutError("worker session exceeded its bounded controller deadline")

    def finish_evidence(self, stream, completed):
        if not self.pod:
            return
        if not completed:
            self.signal(stop=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and self.read_state().get("state") != "finished":
                time.sleep(.5)
        self.copy_events(stream, final=True)
        state = self.read_state()
        save(self.out / "worker-state.json", state)
        if isinstance(state.get("summary"), dict):
            save(self.out / "worker-summary.json", state["summary"])

    def cleanup(self):
        if self.pod and not self.evidence_copied:
            return {"preserved_for_recovery": True, "reason": "worker evidence was not copied; Job deadline/TTL remain"}
        collection_ack = None
        if self.pod:
            try:
                self.signal(collected=True)
                collection_ack = "delivered"
            except Exception:
                # A terminal Pod can no longer accept exec. Exported evidence
                # still permits exact owned-resource cleanup below.
                collection_ack = "not_delivered"
        outcomes = []
        # Delete only exact names whose UID and ownership still match this run.
        for item in reversed(self.created):
            raw = self.client.call(["get", item["kind"], item["name"], "--ignore-not-found", "-o", "json"])
            if not raw.strip():
                continue
            meta = json.loads(raw)["metadata"]
            owned(meta, name=item["name"])
            if meta.get("uid") != item["uid"]:
                raise ValueError("worker cleanup refused a replaced resource")
            self.client.call(["delete", item["kind"], item["name"], "--wait=false"])
            outcomes.append(item["kind"] + "/" + item["name"])
        return {"deleted_owned_resources": outcomes, "worker_collection_ack": collection_ack}


class EvidenceStream:
    def __init__(self, output):
        self.output, self.records = output, []

    def write(self, text):
        row = json.loads(text)
        self.records.append(row)
        self.output.write(text)
        if row["record_type"] in ("phase_start", "phase_end", "run_end"):
            print(json.dumps({k: row[k] for k in ("record_type", "timestamp", "phase", "completed") if k in row}), flush=True)

    def flush(self):
        self.output.flush()


def baseline_check(records):
    rows = [r for r in records if r.get("phase") == "baseline" and r.get("record_type") == "request"]
    missed = sum(r.get("phase") == "baseline" and r.get("record_type") == "arrival_missed" for r in records)
    summary = analyze.summarize_window(rows, missed, availability_slo=.995, latency_slo=.99,
                                       latency_threshold_ms=200, completed=True)
    if (len(rows) != 4500 or missed or summary["F"] or not summary["measurement_valid"]
            or summary["p99_client_duration_ms"] >= 200):
        raise runner.PhaseTransitionError("baseline did not satisfy the predeclared contract")
    return summary


def first_chaos(events, experiment_id, service, mode):
    route, method = ("/payments", "POST") if mode == "latency" else ("/data/health", "GET")
    matched = [e for e in events if e.get("event") == "chaos.applied" and e.get("experiment_id") == experiment_id
               and e.get("service_name") == service and e.get("namespace") == NS and e.get("mode") == mode
               and e.get("route") == route and e.get("method") == method]
    if any(isinstance(e.get("observed_unix_ns"), bool) or not isinstance(e.get("observed_unix_ns"), int)
           or e["observed_unix_ns"] <= 0 for e in matched):
        raise ValueError("chaos log lacks the actual server nanosecond timestamp")
    if any(not isinstance(e.get("trace_id"), str) or not runner.TRACE_ID.fullmatch(e["trace_id"])
           or int(e["trace_id"], 16) == 0 for e in matched):
        # Never skip the first fault to choose a later, easier-to-correlate event.
        raise ValueError("actual chaos logs lack valid trace context")
    return min(matched, key=lambda e: e["observed_unix_ns"]) if matched else None


def ns_timestamp(value):
    seconds, nanos = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def matching_alert(alert, mode):
    labels = alert.get("labels", {})
    return (labels.get("namespace") == NS and labels.get("alertname") ==
            ("ModuleDLatency" if mode == "latency" else "ModuleDErrorRate")
            and labels.get("service") == ("module-d-payments" if mode == "latency" else "module-d-data")
            and labels.get("route") == ("/payments" if mode == "latency" else "/data/health")
            and labels.get("method") == ("POST" if mode == "latency" else "GET"))


def detection_evidence(first, observation, notifications, mode):
    if first is None:
        return {"detection": analyze.analyze_detection(), "notification": {"status": "not_measured"},
                "resolution": {"status": "not_measured"}}
    start = ns_timestamp(first["observed_unix_ns"])
    first_time = analyze.parse_timestamp(start)
    firing = sorted(row["first_observed_firing_at"] for row in observation.get("first_observed_firing", [])
                    if matching_alert(row.get("alert", {}), mode) and row["alert"].get("state") == "firing"
                    and analyze.parse_timestamp(row["first_observed_firing_at"]) >= first_time)
    received, resolved = [], []
    for row in notifications.get("events", []):
        at = row.get("received_at")
        if not at or analyze.parse_timestamp(at) < first_time:
            continue
        for alert in row.get("alerts", []):
            if matching_alert(alert, mode):
                if alert.get("status") == "firing":
                    received.append(at)
                elif alert.get("status") == "resolved":
                    resolved.append(at)
    detected = analyze.analyze_detection(first_chaos_applied_at=start,
        alert_firing_at=min(firing) if firing else None, observed_until=observation.get("ended_at"),
        evidence_source="prometheus_firing_observed")
    # Webhook receipt is notification latency, explicitly separate from firing.
    notification = {"status": "observed" if received else "not_observed", "received_at": min(received) if received else None,
                    "delay_seconds": (analyze.parse_timestamp(min(received)) - first_time).total_seconds() if received else None,
                    "scope": "internal webhook receipt; no email or human acknowledgement"}
    resolution = {"status": "observed" if resolved else "not_observed", "received_at": min(resolved) if resolved else None,
                  "scope": "internal resolved webhook receipt"}
    return {"detection": detected, "notification": notification, "resolution": resolution}


def app_config(mode, experiment_id):
    role, path, method, port = ("payments", "/payments", "POST", 18001) if mode == "latency" else ("data", "/data/health", "GET", 18002)
    body = json.dumps({"order_id": str(uuid.uuid4()), "amount": 100, "currency": "COP"}).encode() if role == "payments" else b"{}"
    return runner.Config(runner.validate_target(f"http://127.0.0.1:{port}", path, "127.0.0.1"), experiment_id, method, body)


def wait_preflight(forwards, out):
    preflight = []
    for mode in ("latency", "error"):
        config = app_config(mode, "preflight-" + uuid.uuid4().hex)
        config.connect_address = runner.resolve_target(config)
        row = runner.request_once(config, {"experiment_id": config.experiment_id, "phase": "preflight",
            "request_id": config.experiment_id, "scheduled_at": runner.utc_now()}, time.monotonic(), time.monotonic() + 15)
        if not row["success"] or row.get("trace_header_matches") is not True:
            raise ValueError("preflight request or returned trace context failed")
        preflight.append(row)
    save(out / "preflight-requests.json", preflight)
    deadline, ingested = time.monotonic() + 90, set()
    while time.monotonic() < deadline:
        forwards.healthy()
        try:
            verify_targets(fetch(19090, "/api/v1/targets"))
            for row in preflight:
                if row["sent_trace_id"] in ingested:
                    continue
                traces = fetch(16686, "/api/traces/" + row["sent_trace_id"])
                if any(t.get("traceID") == row["sent_trace_id"] for t in traces.get("data", [])):
                    save(out / ("preflight-trace-" + row["sent_trace_id"] + ".json"), traces)
                    ingested.add(row["sent_trace_id"])
            if len(ingested) == 2:
                alerts = fetch(19090, "/api/v1/alerts")
                if alerts.get("data", {}).get("alerts"):
                    raise ValueError("preflight found active or pending Module D alerts")
                return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        time.sleep(2)
    raise TimeoutError("healthy scrape and both backend traces were not verified")


def collect_server_events(client, pod, since, out):
    raw = client.call(["logs", pod["name"], "-c", pod["container"], "--since-time=" + since,
                       "--limit-bytes=20000000"], timeout=30)
    events = []
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("event") == "chaos.applied":
            events.append({k: v for k, v in value.items() if k in SAFE_LOG_FIELDS})
    save(out / "server-chaos-events.json", events)
    return events


def collect_metric_history(mode, records):
    """Retain the app-side series separately from client request measurements."""
    service, route, method = (("module-d-payments", "/payments", "POST") if mode == "latency"
                              else ("module-d-data", "/data/health", "GET"))
    scope = '{service=' + json.dumps(service) + ',route=' + json.dumps(route) + ',method=' + json.dumps(method) + '}'
    queries = {
        "request_rate": f"sum(rate(module_d_http_requests_total{scope}[60s]))",
        "p99_server_seconds": f"histogram_quantile(0.99,sum by(le)(rate(module_d_http_request_duration_seconds_bucket{scope}[60s])))",
        "error_rate": f"sum(rate(module_d_http_requests_total{scope[:-1]},status_code=~\"5..\"}}[60s])) / sum(rate(module_d_http_requests_total{scope}[60s]))",
    }
    start = analyze.parse_timestamp(records[0]["timestamp"]).timestamp()
    end = time.time()
    return {"scope": "server wrapper durations and HTTP status; 60s rolling windows, not client latency",
            "missing_5xx_series_note": "error-rate empty before first 5xx means no 5xx series; use request series and client counts",
            "series": {name: {"query": query, "response": fetch(19090, "/api/v1/query_range?" + urlencode(
                {"query": query, "start": start, "end": end, "step": 5}))} for name, query in queries.items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--cluster", default="otel-lab")
    parser.add_argument("--mode", choices=("latency", "error"), required=True)
    parser.add_argument("--load-origin", choices=("port-forward", "in-cluster"), default="port-forward")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    state = (REPO / ".state").resolve()
    if state not in out.parents:
        parser.error("output must be a NEW directory underneath this repository's .state")
    state.mkdir(exist_ok=True)
    lock_path = state / "module-d-session.lock"
    lock = None
    client, forwards, monitor, monitor_log = None, None, None, None
    pods, rollback, issues, stream, result, control = {}, {}, [], None, None, None
    output_created = False
    cluster_worker = None
    experiment_id = "d-" + args.mode + "-" + uuid.uuid4().hex
    started = runner.utc_now()
    try:
        lock = lock_path.open("x", encoding="utf-8")
        lock.write(json.dumps({"experiment_id": experiment_id, "started_at": started})); lock.flush()
        out.mkdir(parents=True, exist_ok=False)
        output_created = True
        if sys.platform != "win32":
            out.chmod(0o700)
        save(out / "contract.json", {"experiment_id": experiment_id, "project": args.project, "namespace": NS,
            "mode": args.mode, "declared_at": started, "phase_seconds": {"baseline": 900, "chaos": 180, "recovery": 300},
            "requests_per_second": 5, "max_concurrency": 20, "availability_target": .995,
            "latency_target": .99, "latency_threshold_ms": 200, "baseline_gate": "4500 requests, no errors/misses, client p99 <200 ms",
            "load_origin": args.load_origin,
            "latency_scope": ("dedicated in-cluster client to Service ClusterIP, including TCP connection and response" if args.load_origin == "in-cluster"
                              else "client includes port-forward/network") + "; Prometheus histogram measures app wrapper duration",
            "ttl_seconds": 180, "transition_note": "phase transitions pause offered arrivals; pod TTL begins at activation and is never extended"})
        client = Kubernetes(args.project, args.location, args.cluster)
        # This check precedes even the initial OFF; never interrupt another run.
        client.require_no_active_workers()
        pods = {name: client.pod(name) for name in PORTS}
        save(out / "pods-before.json", pods)
        for name in ("module-d-payments", "module-d-data"):
            client.control(pods[name], False, experiment_id, args.mode)
        forwards = Forwards(client, pods, out)
        forwards.start()
        if args.load_origin == "in-cluster":
            cluster_worker = InClusterWorker(client, experiment_id, args.mode,
                client.worker_image(pods["module-d-payments"]), out)
            cluster_worker.create()
        wait_preflight(forwards, out)
        monitor_log = (out / "monitor.log").open("x", encoding="utf-8")
        stop_file = out / "stop-monitor"
        monitor = subprocess.Popen([sys.executable, str(ROOT / "monitoring" / "capture.py"),
            "--out", str(out / "monitoring"), "--seconds", "1800", "--poll-seconds", "2",
            "--stop-file", str(stop_file)], stdout=monitor_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while not (out / "monitoring" / "observation.json").exists():
            if monitor.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("monitoring capture did not start")
            time.sleep(.1)
        config = app_config(args.mode, experiment_id)
        target = "module-d-payments" if args.mode == "latency" else "module-d-data"
        def transition(phase):
            nonlocal control
            try:
                forwards.healthy()
                if monitor.poll() is not None:
                    raise RuntimeError("monitor stopped before the experiment")
                verify_targets(fetch(19090, "/api/v1/targets"))
                if phase == "chaos":
                    baseline = baseline_check(stream.records)
                    save(out / "baseline-gate.json", baseline)
                    if fetch(19090, "/api/v1/alerts").get("data", {}).get("alerts"):
                        raise ValueError("baseline has an active/pending monitoring alert")
                    control = client.control(pods[target], True, experiment_id, args.mode)
                    save(out / "activation.json", control)
                    return {"control_enabled": True, "pod_issued_at": control["issued_at"], "pod_expires_at": control["expires_at"]}
                if phase == "recovery":
                    off = client.control(pods[target], False, experiment_id, args.mode)
                    save(out / "deactivation.json", {"observed_at": runner.utc_now(), **off})
                return {"control_enabled": False}
            except Exception as exc:
                issues.append({"stage": "phase_transition_" + phase, "error_type": type(exc).__name__})
                raise runner.PhaseTransitionError("guarded transition failed") from exc

        if cluster_worker:
            stream = SimpleNamespace(records=[])
            result = cluster_worker.run(transition, stream)
        else:
            with (out / "requests.jsonl").open("x", encoding="utf-8") as output:
                stream = EvidenceStream(output)
                result = runner.run(config, stream, phase_hook=transition)
    except (Exception, KeyboardInterrupt) as exc:
        issues.append({"stage": "session", "error_type": type(exc).__name__})
    finally:
        if client:
            for name in ("module-d-payments", "module-d-data"):
                if name in pods:
                    try:
                        rollback[name] = {"verified_off": client.control(pods[name], False, experiment_id, args.mode)}
                    except Exception as exc:
                        rollback[name] = {"verified_off": False, "error_type": type(exc).__name__,
                                          "fallback": "absolute TTL expires without extension; inspect pod identity"}
                        issues.append({"stage": "rollback_" + name, "error_type": type(exc).__name__})
        if output_created:
            try:
                save(out / "rollback.json", {"observed_at": runner.utc_now(), "services": rollback})
            except OSError as exc:
                issues.append({"stage": "rollback_evidence", "error_type": type(exc).__name__})
        if cluster_worker:
            try:
                if stream is None:
                    stream = SimpleNamespace(records=[])
                cluster_worker.finish_evidence(stream, bool(result and result["completed"]))
            except Exception as exc:
                issues.append({"stage": "worker_evidence", "error_type": type(exc).__name__})
        if monitor:
            try:
                (out / "stop-monitor").touch()
                monitor.wait(timeout=25)
                if monitor.returncode:
                    issues.append({"stage": "monitoring", "error_type": "capture_nonzero_exit"})
            except Exception as exc:
                issues.append({"stage": "monitoring", "error_type": type(exc).__name__})
                try:
                    stop_process(monitor)
                except Exception as cleanup_exc:
                    issues.append({"stage": "monitoring_cleanup", "error_type": type(cleanup_exc).__name__})
        if stream and stream.records and client:
            try:
                target = "module-d-payments" if args.mode == "latency" else "module-d-data"
                events = collect_server_events(client, pods[target], started, out)
                first = first_chaos(events, experiment_id, target, args.mode)
                save(out / "first-chaos-applied.json", first)
                if result and result["completed"] and first is None:
                    issues.append({"stage": "evidence", "error_type": "actual_fault_not_observed"})
                observation_path = out / "monitoring" / "observation.json"
                notification_path = out / "monitoring" / "receiver-final.json"
                observation = json.loads(observation_path.read_text()) if observation_path.exists() else {}
                notifications = json.loads(notification_path.read_text()) if notification_path.exists() else {}
                evidence = detection_evidence(first, observation, notifications, args.mode)
                analysis = analyze.analyze_records(stream.records, experiment_id=experiment_id, latency_threshold_ms=200)
                analysis.update(evidence)
                if result and result["completed"]:
                    if not analysis["whole_experiment"]["measurement_valid"]:
                        issues.append({"stage": "analysis", "error_type": "invalid_load_measurement"})
                    recovery = analysis["phases"]["recovery"]
                    if not recovery["measurement_valid"] or recovery["N"] != 1500 or recovery["F"] or recovery["p99_client_duration_ms"] >= 200:
                        issues.append({"stage": "analysis", "error_type": "recovery_not_healthy"})
                analysis["control_timing"] = {"activation": control, "note": "TTL precedes the chaos load window by command/transition time; no silent extension"}
                save(out / "analysis.json", analysis)
                traces = []
                for trace_id in list(dict.fromkeys(e.get("trace_id") for e in events if e.get("experiment_id") == experiment_id))[:3]:
                    if isinstance(trace_id, str) and runner.TRACE_ID.fullmatch(trace_id):
                        value = fetch(16686, "/api/traces/" + trace_id)
                        found = any(t.get("traceID") == trace_id for t in value.get("data", []))
                        save(out / ("chaos-trace-" + trace_id + ".json"), value)
                        traces.append({"trace_id": trace_id, "backend_trace_observed": found})
                save(out / "trace-correlation.json", traces)
                if first is not None and (not traces or not all(t["backend_trace_observed"] for t in traces)):
                    issues.append({"stage": "evidence", "error_type": "affected_backend_trace_not_observed"})
                save(out / "server-metric-history.json", collect_metric_history(args.mode, stream.records))
                save(out / "targets-after.json", fetch(19090, "/api/v1/targets"))
                save(out / "alerts-after.json", fetch(19090, "/api/v1/alerts"))
                save(out / "pods-after.json", {name: client.pod(name) for name in PORTS})
            except Exception as exc:
                issues.append({"stage": "evidence", "error_type": type(exc).__name__})
        if cluster_worker:
            try:
                cleanup = cluster_worker.cleanup()
                save(out / "worker-cleanup.json", cleanup)
                if cleanup.get("preserved_for_recovery"):
                    issues.append({"stage": "worker_cleanup", "error_type": "preserved_for_evidence_recovery"})
            except Exception as exc:
                issues.append({"stage": "worker_cleanup", "error_type": type(exc).__name__})
        if forwards:
            try:
                forwards.close()
            except Exception as exc:
                issues.append({"stage": "port_forward_cleanup", "error_type": type(exc).__name__})
        if monitor_log:
            try:
                monitor_log.close()
            except OSError as exc:
                issues.append({"stage": "monitor_log_cleanup", "error_type": type(exc).__name__})
        if lock:
            summary = {"experiment_id": experiment_id, "ended_at": runner.utc_now(),
                "load_completed": bool(result and result["completed"]), "issues": issues,
                "rollback_confirmed": len(rollback) == 2 and all(isinstance(v["verified_off"], dict) and
                    v["verified_off"].get("enabled") is False for v in rollback.values()),
                "status": "EVIDENCE_VALIDATED" if result and result["completed"] and not issues else "INCOMPLETE_REVIEW_REQUIRED"}
            try:
                if output_created:
                    save(out / "session.json", summary)
                print(json.dumps(summary), flush=True)
            finally:
                lock.close()
                lock_path.unlink()
    return 0 if result and result["completed"] and not issues else 2


if __name__ == "__main__":
    raise SystemExit(main())
