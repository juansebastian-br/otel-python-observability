"""Clone only approved app images/source into an isolated, ephemeral Module D lab.

Cloud Shell: python module-d/gcp/deploy.py --project PROJECT
Add --apply only after reviewing the private .state/module-d output.
This script never changes the original namespace and never copies its Secrets.
"""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
NS = "observability-chaos-d"
SOURCE_NS = "observability-lab"
OWNER = "otel-module-d"
LABELS = {"managed-by": OWNER, "app.kubernetes.io/part-of": "module-d"}
APP_DEPLOYMENTS = {"payments": "payments-service", "data": "data-service"}
SOURCE_PATHS = {"main.py": "/app/app/main.py", "otel.py": "/app/app/otel.py"}
ALLOWED_KINDS = {"Namespace", "ConfigMap", "Secret", "Deployment", "Service", "NetworkPolicy"}
DIGEST = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run(command, *, payload=None):
    result = subprocess.run(command, input=payload, text=True, capture_output=True, check=False)
    if result.returncode:
        # Do not echo API bodies or stderr: admission errors can contain Secrets.
        raise RuntimeError(f"Command failed ({result.returncode}): {command[0]} {command[1]}; inspect the resource status separately")
    return result.stdout


class Client:
    def __init__(self, project, location="us-central1", cluster="otel-lab"):
        current = run(["gcloud", "config", "get-value", "project"]).strip()
        if current != project:
            raise ValueError("The active gcloud project does not match --project")
        expected = f"gke_{project}_{location}_{cluster}"
        context = run(["kubectl", "config", "current-context"]).strip()
        if context != expected:
            raise ValueError("The current kubectl context does not match the declared GKE project/location/cluster")
        self.context = context

    def call(self, args, *, payload=None):
        return run(["kubectl", "--context", self.context] + args, payload=payload)

    def get(self, kind, name=None, namespace=None, *, metadata_only=False):
        args = ["get", kind] + ([name] if name else [])
        if namespace:
            args += ["-n", namespace]
        args += ["--ignore-not-found", "-o", "jsonpath={.metadata}" if metadata_only else "json"]
        raw = self.call(args).strip()
        return json.loads(raw) if raw else None

    def apply(self, manifest):
        validate_manifest(manifest)
        self.call(["apply", "--server-side", "--field-manager=otel-module-d", "-f", "-"],
                  payload=json.dumps(manifest))


def require_owned(metadata):
    if not metadata or any(metadata.get("labels", {}).get(k) != v for k, v in LABELS.items()):
        raise ValueError("Existing sandbox namespace/resource is not owned by Module D; refusing to adopt it")


def adapt_data_source(source):
    replacements = [
        ('engines = {name: db_engine(prefix) for name, prefix in (("gcp", "GCP_DB"), ("aws", "AWS_DB"))}',
         'engines = {name: db_engine(prefix) for name, prefix in (("gcp", "GCP_DB"),)}'),
        ('if set(engines) != {"gcp", "aws"}:', 'if set(engines) != {"gcp"}:'),
        ('"Both GCP and AWS connections must be configured"', '"The isolated GCP sandbox connection must be configured"'),
        ('span.set_attribute("cloud.provider", "gcp" if name == "gcp" else "aws")',
         'span.set_attribute("cloud.provider", "gcp")'),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise ValueError("Live data-service source differs from the reviewed GCP adaptation; manual review required")
        source = source.replace(old, new, 1)
    ast.parse(source)
    if "AWS_DB" in source:
        raise ValueError("An unreviewed AWS database reference remains in the sandbox copy")
    return source


def choose_source(deployment, replica_sets, pods):
    """Only a ready pod owned through an RS by the declared deployment qualifies."""
    meta, spec = deployment["metadata"], deployment["spec"]
    if meta.get("namespace") != SOURCE_NS or meta.get("name") not in APP_DEPLOYMENTS.values():
        raise ValueError("Unexpected source deployment")
    desired = spec.get("replicas", 1)
    if desired < 1 or deployment.get("status", {}).get("readyReplicas", 0) != desired:
        raise ValueError("Source deployment must be fully ready before cloning")
    containers = [c for c in spec["template"]["spec"]["containers"] if c["name"] != "istio-proxy"]
    if len(containers) != 1:
        raise ValueError("Source must have exactly one business container; review additional containers")
    container = containers[0]
    owned_rs = {r["metadata"]["uid"] for r in replica_sets
                if any(o.get("uid") == meta["uid"] and o.get("kind") == "Deployment"
                       for o in r["metadata"].get("ownerReferences", []))}
    selected = []
    for pod in pods:
        if pod["metadata"].get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running":
            continue
        if not any(o.get("uid") in owned_rs and o.get("kind") == "ReplicaSet"
                   for o in pod["metadata"].get("ownerReferences", [])):
            continue
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])):
            continue
        status = next((c for c in pod["status"].get("containerStatuses", []) if c["name"] == container["name"]), {})
        if not status.get("ready") or status.get("image") != container["image"]:
            continue
        image = re.sub(r"^[a-zA-Z0-9+.-]+://", "", status.get("imageID", ""))
        if not DIGEST.fullmatch(image):
            raise ValueError("Source pod is missing a canonical immutable imageID")
        selected.append({"pod": pod["metadata"]["name"], "container": container["name"], "image": image,
                         "deployment": meta["name"], "deployment_uid": meta["uid"]})
    if len(selected) != desired or len({p["image"] for p in selected}) != 1:
        raise ValueError("Ready source pods are missing, rolling, or disagree on the immutable image digest")
    return sorted(selected, key=lambda item: item["pod"])[0]


def snapshot(client):
    result = []
    for kind in ("deployment", "service", "configmap", "networkpolicy"):
        value = client.get(kind, namespace=SOURCE_NS) or {"items": []}
        for item in value.get("items", []):
            meta = item["metadata"]
            result.append({"kind": item["kind"], "name": meta["name"], "uid": meta["uid"],
                "generation": meta.get("generation"),
                "definition_sha256": sha(json.dumps(item.get("spec", item.get("data", {})), sort_keys=True))})
    return sorted(result, key=lambda item: (item["kind"], item["name"]))


def metadata(name, namespace=NS):
    return {"name": name, "namespace": namespace, "labels": dict(LABELS)}


def configmap(name, data):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name), "data": data}


def security(uid=1000):
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
        "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"}}


def workload(name, image, ports, *, env=None, command=None, args=None, mounts=None,
             volumes=None, uid=1000, memory="256Mi", readiness=None):
    labels = {**LABELS, "app": name}
    container = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": n, "containerPort": p} for n, p in ports],
        "securityContext": security(uid), "resources": {"requests": {"cpu": "100m", "memory": memory,
            "ephemeral-storage": "1Gi"}, "limits": {"memory": memory, "ephemeral-storage": "1Gi"}},
        "readinessProbe": readiness or {"tcpSocket": {"port": ports[0][1]}, "periodSeconds": 5},
        "volumeMounts": mounts or []}
    for key, value in (("env", env), ("command", command), ("args", args)):
        if value is not None:
            container[key] = value
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(name),
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels, "annotations": {"sidecar.istio.io/inject": "false"}},
                "spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
                    "securityContext": {"fsGroup": uid}, "containers": [container], "volumes": volumes or []}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": metadata(name), "spec": {
        "type": "ClusterIP", "selector": {"app": name},
        "ports": [{"name": n, "port": p, "targetPort": p} for n, p in ports]}}
    svc["metadata"]["annotations"] = {"cloud.google.com/neg": '{"ingress":false}'}
    return [deployment, svc]


def secret_ref(name, key):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": "module-d-db", "key": key}}}


def new_secret():
    password = secrets.token_urlsafe(36)
    return {"apiVersion": "v1", "kind": "Secret", "metadata": metadata("module-d-db"),
        "type": "Opaque", "stringData": {"password": password, "user": "module_d", "database": "module_d",
            "url": f"postgresql+psycopg://module_d:{password}@module-d-postgres:5432/module_d"}}


def monitoring_module():
    spec = importlib.util.spec_from_file_location("module_d_monitoring_render", ROOT / "monitoring" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def monitoring_config(images, threshold):
    return {"namespace": NS, "receiver_image": images["payments"], "latency_threshold_ms": threshold,
        "minimum_samples": 20, "payments": {"service": "module-d-payments", "route": "/payments",
            "method": "POST", "target": "module-d-payments:8001"}, "data": {"service": "module-d-data",
            "route": "/data/health", "method": "GET", "target": "module-d-data:8002"}}


def build(images, sources, threshold=200):
    for role in APP_DEPLOYMENTS:
        if not DIGEST.fullmatch(images[role]):
            raise ValueError("Apps must use immutable image digests")
        for name in SOURCE_PATHS:
            ast.parse(sources[role][name])
    rules = monitoring_module().build(monitoring_config(images, threshold))
    runtime = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "runtime").glob("*.py"))}
    if not {"__init__.py", "chaos.py", "entrypoint.py"}.issubset(runtime):
        raise ValueError("The complete reviewed runtime package is required")
    objects = [configmap("module-d-runtime", runtime),
               configmap("module-d-db-init", {"init.sql": (REPO / "db-init.sql").read_text(encoding="utf-8")})]
    objects += workload("module-d-postgres", "postgres:16-alpine", [("postgres", 5432)], uid=70,
        env=[secret_ref("POSTGRES_USER", "user"), secret_ref("POSTGRES_PASSWORD", "password"),
             secret_ref("POSTGRES_DB", "database"), {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}],
        mounts=[{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                {"name": "init", "mountPath": "/docker-entrypoint-initdb.d", "readOnly": True}],
        volumes=[{"name": "data", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "init", "configMap": {"name": "module-d-db-init"}}],
        readiness={"exec": {"command": ["pg_isready", "-U", "module_d", "-d", "module_d"]}, "periodSeconds": 5})
    for role, port, method, route in (("payments", 8001, "POST", "/payments"), ("data", 8002, "GET", "/data/health")):
        name = "module-d-" + role
        objects.append(configmap(name + "-source", {"__init__.py": "", **sources[role]}))
        env = [{"name": k, "value": v} for k, v in {
            "PYTHONPATH": "/opt/module-d:/app", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "DEPLOYMENT_ENVIRONMENT": "sandbox", "MODULE_D_SERVICE_NAME": name, "MODULE_D_ROLE": role,
            "MODULE_D_APP": "app.main:app", "MODULE_D_ROUTES": json.dumps({method: [route]}),
            "OTEL_SERVICE_NAME": name, "OTEL_EXPORTER_OTLP_ENDPOINT": "otel-collector:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc", "OTEL_EXPORTER_OTLP_INSECURE": "true",
            "OTEL_TRACES_SAMPLER": "always_on", "OTEL_SDK_DISABLED": "false", "PAYMENTS_FAILURE_RATE": "0",
            "GCP_DB_HOST": "module-d-postgres", "GCP_DB_PORT": "5432", "GCP_DB_SSL_MODE": ""}.items()]
        env += [{"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
            secret_ref("DATABASE_URL", "url"), secret_ref("GCP_DB_USER", "user"),
            secret_ref("GCP_DB_PASSWORD", "password"), secret_ref("GCP_DB_DATABASE", "database")]
        pair = workload(name, images[role], [("http", port)], env=env,
            command=["python", "-m", "uvicorn"], args=["module_d_runtime.entrypoint:app", "--host", "0.0.0.0", "--port", str(port)],
            mounts=[{"name": "source", "mountPath": "/opt/module-d/app", "readOnly": True},
                {"name": "runtime", "mountPath": "/opt/module-d/module_d_runtime", "readOnly": True},
                {"name": "chaos", "mountPath": "/var/run/chaos"}],
            volumes=[{"name": "source", "configMap": {"name": name + "-source"}},
                {"name": "runtime", "configMap": {"name": "module-d-runtime"}},
                {"name": "chaos", "emptyDir": {"sizeLimit": "1Mi"}}])
        pair[0]["spec"]["template"]["spec"]["containers"][0]["workingDir"] = "/opt/module-d"
        pair[0]["spec"]["template"]["spec"]["initContainers"] = [{"name": "wait-for-sandbox-db",
            "image": "postgres:16-alpine", "command": ["sh", "-c", "until pg_isready -h module-d-postgres -U module_d -d module_d; do sleep 2; done"],
            "securityContext": security(70), "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "64Mi"}}}]
        objects += pair
    jaeger = {"extensions": {"jaeger_storage": {"backends": {"sandbox": {"memory": {"max_traces": 20000}}}},
        "jaeger_query": {"storage": {"traces": "sandbox"}, "http": {"endpoint": "0.0.0.0:16686"}}},
        "receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {"batch": {}}, "exporters": {"jaeger_storage_exporter": {"trace_storage": "sandbox"}},
        "service": {"extensions": ["jaeger_storage", "jaeger_query"], "pipelines": {"traces": {
            "receivers": ["otlp"], "processors": ["batch"], "exporters": ["jaeger_storage_exporter"]}}}}
    collector = {"receivers": {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {"memory_limiter": {"check_interval": "1s", "limit_mib": 350, "spike_limit_mib": 75}, "batch": {}},
        "exporters": {"otlp/jaeger": {"endpoint": "module-d-jaeger:4317", "tls": {"insecure": True}}, "debug": {"verbosity": "basic"}},
        "service": {"pipelines": {signal: {"receivers": ["otlp"], "processors": ["memory_limiter", "batch"],
            "exporters": ["otlp/jaeger" if signal == "traces" else "debug"]} for signal in ("traces", "metrics", "logs")}}}
    for name, image, ports, config in (("module-d-jaeger", "jaegertracing/jaeger:2.21.0",
        [("query", 16686), ("otlp-grpc", 4317), ("otlp-http", 4318)], jaeger),
        ("otel-collector", "otel/opentelemetry-collector-contrib:0.161.0", [("otlp-grpc", 4317), ("otlp-http", 4318)], collector)):
        objects.append(configmap(name + "-config", {"config.yaml": json.dumps(config)}))
        objects += workload(name, image, ports, uid=10001, memory="1Gi" if name == "module-d-jaeger" else "512Mi",
            args=["--config=/etc/module-d/config.yaml"], mounts=[{"name": "config", "mountPath": "/etc/module-d", "readOnly": True}],
            volumes=[{"name": "config", "configMap": {"name": name + "-config"}}])
    same_namespace = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}}}
    objects += [{"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata("module-d-default-deny"),
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata("module-d-internal-and-dns"),
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [{"from": [same_namespace]}],
            "egress": [{"to": [same_namespace]}, {"to": [
                {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                 "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}},
                {"ipBlock": {"cidr": "169.254.20.10/32"}}, {"ipBlock": {"cidr": "169.254.169.254/32"}}],
                "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}]}}]
    objects += rules["manifest"]["items"]
    # Mounted ConfigMaps update asynchronously; restart each affected process when
    # its configuration/source changes so imports and rules cannot remain stale.
    maps = {item["metadata"]["name"]: item["data"] for item in objects if item["kind"] == "ConfigMap"}
    for item in objects:
        if item["kind"] == "Deployment":
            template = item["spec"]["template"]
            used = {volume["configMap"]["name"]: maps[volume["configMap"]["name"]]
                    for volume in template["spec"].get("volumes", []) if "configMap" in volume}
            template["metadata"]["annotations"]["module-d/config-sha256"] = sha(json.dumps(used, sort_keys=True))
    manifest = {"apiVersion": "v1", "kind": "List", "items": objects}
    validate_manifest(manifest)
    return manifest, rules


def validate_manifest(manifest):
    items = manifest.get("items", []) if manifest.get("kind") == "List" else [manifest]
    if not items:
        raise ValueError("Empty manifest")
    seen = set()
    for item in items:
        kind, meta = item.get("kind"), item.get("metadata", {})
        if kind not in ALLOWED_KINDS or not meta.get("name"):
            raise ValueError("Unsupported resource kind or missing name")
        require_owned(meta)
        if (kind == "Namespace" and meta["name"] != NS) or (kind != "Namespace" and meta.get("namespace") != NS):
            raise ValueError("A resource targets a namespace outside the Module D sandbox")
        key = (kind, meta["name"])
        if key in seen:
            raise ValueError("Duplicate resource")
        seen.add(key)
        if kind == "Service" and item["spec"].get("type", "ClusterIP") != "ClusterIP":
            raise ValueError("Only internal ClusterIP Services are allowed")
        if kind == "Deployment":
            pod = item["spec"]["template"]["spec"]
            if pod.get("automountServiceAccountToken") is not False or pod.get("hostNetwork") or pod.get("hostPID"):
                raise ValueError("Pod identity/host access violates the sandbox contract")
            if any("hostPath" in v for v in pod.get("volumes", [])):
                raise ValueError("hostPath is forbidden")
            for c in pod["containers"] + pod.get("initContainers", []):
                sec = c.get("securityContext", {})
                if not sec.get("runAsNonRoot") or sec.get("allowPrivilegeEscalation") is not False or sec.get("privileged") or sec.get("capabilities", {}).get("add"):
                    raise ValueError("Privileged containers are forbidden")


def preflight_resources(client, manifest):
    existing_namespace = client.get("namespace", NS, metadata_only=True)
    if existing_namespace:
        require_owned(existing_namespace)
    for item in manifest["items"]:
        existing = client.get(item["kind"], item["metadata"]["name"], NS, metadata_only=True)
        if existing:
            require_owned(existing)
    existing_secret = client.get("secret", "module-d-db", NS, metadata_only=True)
    if existing_secret:
        require_owned(existing_secret)
    return bool(existing_secret)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--cluster", default="otel-lab")
    parser.add_argument("--location", default="us-central1")
    parser.add_argument("--latency-threshold-ms", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path(".state/module-d"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / ".gitignore").write_text("*\n", encoding="utf-8")
    client = Client(args.project, args.location, args.cluster)
    existing_namespace = client.get("namespace", NS, metadata_only=True)
    if existing_namespace:
        require_owned(existing_namespace)
    before = snapshot(client)
    (out / "originals-before.json").write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    rs = (client.get("replicaset", namespace=SOURCE_NS) or {"items": []})["items"]
    pods = (client.get("pod", namespace=SOURCE_NS) or {"items": []})["items"]
    inventory, sources, hashes = {}, {}, {}
    for role, deployment_name in APP_DEPLOYMENTS.items():
        dep = client.get("deployment", deployment_name, SOURCE_NS)
        if not dep:
            raise ValueError("Required business deployment is absent")
        selected = choose_source(dep, rs, pods)
        inventory[role] = selected
        sources[role], hashes[role] = {}, {}
        for filename, source_path in SOURCE_PATHS.items():
            original = client.call(["exec", "-n", SOURCE_NS, selected["pod"], "-c", selected["container"],
                "--", "python", "-c", "from pathlib import Path; print(Path(" + repr(source_path) + ").read_text(), end='')"])
            ast.parse(original)
            adapted = adapt_data_source(original) if role == "data" and filename == "main.py" else original
            sources[role][filename] = adapted
            hashes[role][filename] = {"original_sha256": sha(original), "sandbox_sha256": sha(adapted)}
    images = {role: value["image"] for role, value in inventory.items()}
    manifest, monitoring = build(images, sources, args.latency_threshold_ms)
    config = monitoring_config(images, args.latency_threshold_ms)
    contract_path = out / "monitoring" / "contract.json"
    if contract_path.exists() and json.loads(contract_path.read_text(encoding="utf-8"))["config"] != config:
        raise ValueError("Predeclared monitoring contract changed; choose a new --out and baseline")
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    if not contract_path.exists():
        contract_path.write_text(json.dumps({"declared_at": utc(), "config": config, "availability_target": 0.995,
            "latency_target": 0.99, "status": "CONFIGURATION_ONLY_NOT_EXECUTED"}, indent=2) + "\n", encoding="utf-8")
    for filename, value in (("manifest.json", manifest), ("source-hashes.json", hashes), ("inventory.json", inventory)):
        (out / filename).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    for name, value in monitoring.items():
        (out / "monitoring" / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    has_secret = preflight_resources(client, manifest)
    status = "RENDERED_NOT_APPLIED"
    try:
        if args.apply:
            status = "APPLY_STARTED_NOT_COMPLETED"
            namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS,
                "labels": {**LABELS, "istio-injection": "disabled"}}}
            client.apply(namespace)
            # Apply isolation first, before any new Pod can run.
            client.apply({"apiVersion": "v1", "kind": "List", "items": [r for r in manifest["items"] if r["kind"] == "NetworkPolicy"]})
            if not has_secret:
                client.apply(new_secret())
            client.apply(manifest)
            status = "APPLIED_READINESS_NOT_YET_VERIFIED"
    finally:
        after = snapshot(client)
        (out / "originals-after.json").write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
        receipt = {"at": utc(), "status": status, "project": args.project, "namespace": NS,
            "original_resources_unchanged": before == after, "resources": len(manifest["items"]),
            "source_sha256": hashes, "secret_values_saved": False,
            "note": "Application source is a private sandbox copy. Readiness, baseline and chaos have not been established by this deploy command."}
        (out / "deployment-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        if before != after:
            raise RuntimeError("Original resources changed during deployment; stop and inspect the before/after snapshots")
    print(json.dumps({"status": status, "out": str(out), "namespace": NS, "resources": len(manifest["items"]),
        "original_resources_unchanged": True, "secret_values_saved": False}))


if __name__ == "__main__":
    main()
