"""Bounded in-cluster HTTP load worker; no credentials or chaos activation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import time

from . import analyze, runner

CONFIG_FIELDS = {"experiment_id", "mode", "target_url", "path", "method", "body"}
PHASES = ("baseline", "chaos", "recovery")
MAX_JSON_BYTES = 65536


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def read_json(path, maximum=MAX_JSON_BYTES):
    with Path(path).open("rb") as handle:
        raw = handle.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("JSON file exceeds the bounded input size")
    return strict_json(raw.decode("utf-8"))


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, allow_nan=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_config(value, environment=None):
    environment = os.environ if environment is None else environment
    if environment.get("POD_NAMESPACE") != runner.NAMESPACE or environment.get("DEPLOYMENT_ENVIRONMENT") != "sandbox":
        raise ValueError("worker requires the dedicated sandbox namespace and environment")
    if not isinstance(value, dict) or set(value) != CONFIG_FIELDS:
        raise ValueError("worker configuration must contain exactly the reviewed fields")
    identifier, mode = value["experiment_id"], value["mode"]
    if not isinstance(identifier, str) or not runner.SAFE_ID.fullmatch(identifier) or mode not in ("latency", "error"):
        raise ValueError("invalid experiment identity or mode")
    role, path, method, port = (("payments", "/payments", "POST", 8001) if mode == "latency"
                                else ("data", "/data/health", "GET", 8002))
    hosts = [f"module-d-{role}.{runner.NAMESPACE}.svc" + suffix for suffix in ("", ".cluster.local")]
    allowed_origins = {f"http://{host}:{port}" for host in hosts}
    if value["target_url"] not in allowed_origins or value["path"] != path or value["method"] != method:
        raise ValueError("target must be the exact sandbox service, port, route and method for this mode")
    if not isinstance(value["body"], dict):
        raise ValueError("body must be a JSON object")
    body = json.dumps(value["body"], allow_nan=False, separators=(",", ":")).encode("utf-8")
    host = value["target_url"].split("://", 1)[1].rsplit(":", 1)[0]
    target = runner.validate_target(value["target_url"], path, host)
    config = runner.Config(target, identifier, method, body, rate=5.0, max_workers=20,
        timeout_seconds=10.0, baseline_seconds=900.0, chaos_seconds=180.0,
        recovery_seconds=300.0, max_start_lag_seconds=0.1)
    config.validate()
    return config


def phase_summary(records, phase):
    rows = [r for r in records if r.get("record_type") == "request" and r.get("phase") == phase]
    missed = sum(r.get("record_type") == "arrival_missed" and r.get("phase") == phase for r in records)
    return {"N": len(rows), "F": sum(r.get("success") is not True for r in rows),
        "arrival_missed": missed,
        "p99_client_duration_ms": analyze.p99_nearest_rank([r["latency_ms"] for r in rows])}


def require_healthy_baseline(records):
    rows = [r for r in records if r.get("record_type") == "request" and r.get("phase") == "baseline"]
    missed = sum(r.get("record_type") == "arrival_missed" and r.get("phase") == "baseline" for r in records)
    summary = analyze.summarize_window(rows, missed, availability_slo=.995,
        latency_slo=.99, latency_threshold_ms=200, completed=True)
    if (len(rows) != 4500 or missed or summary["F"] or not summary["measurement_valid"]
            or summary["p99_client_duration_ms"] >= 200):
        raise runner.PhaseTransitionError("baseline rejected before the chaos barrier")
    return summary


class EvidenceStream:
    def __init__(self, output, out, experiment_id):
        self.output, self.out, self.experiment_id = output, Path(out), experiment_id
        self.records, self.phase = [], None
        self.since_progress, self.last_progress = 0, time.monotonic()

    def state(self, state, phase=None, **extra):
        phase = self.phase if phase is None else phase
        value = {"experiment_id": self.experiment_id, "state": state, "phase": phase,
            "observed_at": runner.utc_now(), **phase_summary(self.records, phase), **extra}
        atomic_json(self.out / "phase-state.json", value)
        self.last_progress, self.since_progress = time.monotonic(), 0
        return value

    def write(self, text):
        row = strict_json(text)
        if row.get("experiment_id") != self.experiment_id:
            raise ValueError("mixed experiment evidence")
        self.output.write(text)
        self.output.flush()
        self.records.append(row)
        if row["record_type"] == "phase_start":
            self.phase = row["phase"]
            self.state("running")
        elif row["record_type"] in ("request", "arrival_missed"):
            self.since_progress += 1
            if self.phase and (self.since_progress >= 100 or time.monotonic() - self.last_progress >= 20):
                self.state("running")
        elif row["record_type"] == "phase_end" and self.phase:
            self.state("running")

    def flush(self):
        self.output.flush()


def wait_release(out, experiment_id, phase, stop_event, *, timeout=None,
                 monotonic=time.monotonic, wait=None):
    if phase not in PHASES:
        raise runner.PhaseTransitionError("unknown phase")
    timeout = (180 if phase == "baseline" else 60) if timeout is None else timeout
    deadline = monotonic() + timeout
    wait = stop_event.wait if wait is None else wait
    release = Path(out) / ("release-" + phase + ".json")
    expected = {"experiment_id": experiment_id, "phase": phase, "action": "continue"}
    while True:
        if stop_event.is_set() or (Path(out) / "stop").exists():
            stop_event.set()
            raise runner.PhaseTransitionError("worker stopped before phase release")
        if monotonic() >= deadline:
            raise runner.PhaseTransitionError("phase release deadline expired")
        if release.exists():
            try:
                value = read_json(release, 4096)
            except (OSError, UnicodeError, ValueError) as exc:
                raise runner.PhaseTransitionError("invalid phase release") from exc
            if value != expected or not isinstance(value, dict) or set(value) != set(expected):
                raise runner.PhaseTransitionError("phase release identity does not match")
            return
        wait(min(.1, max(0, deadline - monotonic())))


def wait_for_collection(out, experiment_id, *, timeout=120, monotonic=time.monotonic, sleep=time.sleep):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        marker = Path(out) / "collected"
        if marker.exists():
            try:
                if read_json(marker, 4096) == {"experiment_id": experiment_id}:
                    return True
            except (OSError, UnicodeError, ValueError):
                pass
        sleep(min(.2, max(0, deadline - monotonic())))
    return False


def run_worker(config, out, *, run=runner.run, collection_timeout=120):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if any((out / name).exists() for name in ("events.jsonl", "phase-state.json", "worker-summary.json")):
        raise ValueError("worker will not overwrite previous evidence")
    stop_event, watcher_done = threading.Event(), threading.Event()
    def watch_stop():
        while not watcher_done.wait(.1):
            if (out / "stop").exists():
                stop_event.set()
                return
    watcher = threading.Thread(target=watch_stop, daemon=True, name="module-d-stop")
    result, error_type, stream = None, None, None
    watcher.start()
    try:
        with (out / "events.jsonl").open("x", encoding="utf-8", newline="\n") as output:
            stream = EvidenceStream(output, out, config.experiment_id)
            def transition(phase):
                baseline = require_healthy_baseline(stream.records) if phase == "chaos" else None
                stream.phase = phase
                stream.state("waiting", baseline=baseline)
                wait_release(out, config.experiment_id, phase, stop_event)
                stream.state("running")
                return {"released_by_controller": True, "fault_activation": "external_controller_only"}
            try:
                result = run(config, stream, stop_event=stop_event, phase_hook=transition)
            except (Exception, KeyboardInterrupt) as exc:
                error_type = type(exc).__name__
                stop_event.set()
                starts = [r for r in stream.records if r.get("record_type") == "run_start"]
                ends = [r for r in stream.records if r.get("record_type") == "run_end"]
                if len(starts) == 1 and not ends:
                    requests = [r for r in stream.records if r.get("record_type") == "request"]
                    missed = sum(r.get("record_type") == "arrival_missed" for r in stream.records)
                    result = {"record_type": "run_end", "experiment_id": config.experiment_id,
                        "timestamp": runner.utc_now(), "completed": False, "interrupted": isinstance(exc, KeyboardInterrupt),
                        "attempted": len(requests), "successful": sum(r.get("success") is True for r in requests),
                        "arrival_missed": missed, "planned_arrivals": starts[0]["planned_arrivals"],
                        "unissued_after_stop": starts[0]["planned_arrivals"] - len(requests) - missed,
                        "worker_error_type": error_type}
                    stream.write(json.dumps(result, separators=(",", ":")) + "\n")
    finally:
        watcher_done.set()
        watcher.join(timeout=1)
    summary = {"experiment_id": config.experiment_id, "finished_at": runner.utc_now(),
        "run_end": result, "error_type": error_type, "stop_observed": stop_event.is_set(),
        "completed": bool(result and result.get("completed") is True),
        "phases": {phase: phase_summary(stream.records, phase) for phase in PHASES} if stream else {}}
    atomic_json(out / "worker-summary.json", summary)
    if stream:
        stream.state("finished", phase="complete", summary=summary)
    else:
        atomic_json(out / "phase-state.json", {"experiment_id": config.experiment_id,
            "state": "finished", "phase": "complete", "summary": summary})
    print(json.dumps({"event": "worker_finished", "experiment_id": config.experiment_id,
        "completed": summary["completed"], "error_type": error_type}), flush=True)
    wait_for_collection(out, config.experiment_id, timeout=collection_timeout)
    return 0 if summary["completed"] and error_type is None else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/config/worker.json"))
    parser.add_argument("--out", type=Path, default=Path("/evidence"))
    args = parser.parse_args(argv)
    try:
        config = validate_config(read_json(args.config))
        return run_worker(config, args.out)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"event": "worker_startup_failed", "error_type": type(exc).__name__}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
