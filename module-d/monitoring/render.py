"""Render isolated Kubernetes monitoring resources. No cloud mutations or IAM."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
NS = "observability-chaos-d"
GROUP = "service, route, method"
OWNER = "otel-module-d"


def selector(service, route, method, extra=""):
    return '{service=' + json.dumps(service) + ',route=' + json.dumps(route) + ',method=' + json.dumps(method) + extra + '}'


def queries(service, route, method):
    scope = selector(service, route, method)
    requests = f"sum by ({GROUP}) (increase(module_d_http_requests_total{scope}[60s]))"
    error_scope = selector(service, route, method, ',status_code=~"5.."')
    raw_errors = f"sum by ({GROUP}) (increase(module_d_http_requests_total{error_scope}[60s]))"
    # A valid observed request series can have no 5xx series yet. Fill only that
    # case with zero; a missing request series remains missing, never healthy.
    errors = f"(({raw_errors}) or on ({GROUP}) (({requests}) * 0))"
    p99 = f"histogram_quantile(0.99, sum by (le, {GROUP}) (rate(module_d_http_request_duration_seconds_bucket{scope}[60s])))"
    return {"requests_60s": requests, "errors_60s": errors,
        "error_rate_60s": f"({errors}) / ({requests})", "p99_seconds_60s": p99}


def make_rule(name, expression, signal, description):
    return {"alert": name, "expr": expression, "for": "30s",
        "labels": {"namespace": NS, "managed_by": OWNER, "severity": "warning", "signal": signal},
        "annotations": {"summary": "Module D: " + description,
            "description": 'Service {{ $labels.service }}, route {{ $labels.route }}, method {{ $labels.method }}; observed {{ $value }}. Window 60s, hold 30s.',
            "runbook": "Confirm the isolated pod and experiment. Disable /var/run/chaos/control.json or let its absolute TTL expire. Correlate chaos.applied and trace_id in that pod's JSON logs; preserve baseline, firing/receipt time and recovery. Never change shared C/B resources."}}


def build(config):
    if config["namespace"] != NS:
        raise ValueError("Monitoring is restricted to observability-chaos-d")
    if not 50 <= config["latency_threshold_ms"] <= 5000 or config["minimum_samples"] < 20:
        raise ValueError("Declare latency threshold 50..5000 ms and at least 20 samples before baseline")
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", config["receiver_image"]):
        raise ValueError("Receiver must reuse an approved Python image by immutable digest")
    for role in ("payments", "data"):
        for key in ("service", "route", "method", "target"):
            if not config[role].get(key):
                raise ValueError("Missing observed target contract")
        if config[role]["service"] != "module-d-" + role:
            raise ValueError("Metrics must belong to module-d-payments/module-d-data only")
        target = config[role]["target"]
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?:[0-9]{1,5}", target):
            raise ValueError("Use same-namespace short Service:port targets; external/shared targets are forbidden")
        if not config[role]["route"].startswith("/") or config[role]["route"] in ("/health", "/metrics", "/__module_d/metrics"):
            raise ValueError("Choose the exact business route")
        if config[role]["method"] not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("Unsupported request method")
    qs = {role: queries(*(config[role][k] for k in ("service", "route", "method"))) for role in ("payments", "data")}
    threshold = config["latency_threshold_ms"] / 1000
    latency = f"({qs['payments']['p99_seconds_60s']} > {threshold:g}) and on ({GROUP}) ({qs['payments']['requests_60s']} >= {config['minimum_samples']})"
    errors = f"({qs['data']['error_rate_60s']} > 0.005) and on ({GROUP}) ({qs['data']['requests_60s']} >= {config['minimum_samples']})"
    rules = {"groups": [{"name": "module-d", "interval": "5s", "rules": [
        make_rule("ModuleDLatency", latency, "latency", f"p99 above {threshold:g} seconds"),
        make_rule("ModuleDErrorRate", errors, "error_rate", "5xx rate above 0.5% (availability target 99.5%)"),
        {"alert": "ModuleDScrapeUnavailable", "expr": 'up{job="module-d-apps"} == 0', "for": "30s",
         "labels": {"namespace": NS, "managed_by": OWNER, "severity": "warning", "signal": "telemetry"},
         "annotations": {"summary": "Module D scrape unavailable", "description": "Telemetry unavailable; do not interpret absence of series as a healthy application."}}]}]}
    prom = {"global": {"scrape_interval": "5s", "scrape_timeout": "4s", "evaluation_interval": "5s",
        "external_labels": {"namespace": NS, "managed_by": OWNER}},
        "rule_files": ["/etc/prometheus/rules.yaml"],
        "alerting": {"alertmanagers": [{"static_configs": [{"targets": ["module-d-alertmanager:9093"]}]}]},
        "scrape_configs": [{"job_name": "module-d-apps", "metrics_path": "/__module_d/metrics", "honor_labels": False,
            "static_configs": [{"targets": [config[role]["target"]], "labels": {"sandbox_role": role}} for role in ("payments", "data")]}]}
    am = {"global": {"resolve_timeout": "1m"}, "route": {"receiver": "module-d-evidence",
        "group_by": ["alertname", "service", "route", "method"], "group_wait": "0s", "group_interval": "5s", "repeat_interval": "5m"},
        "receivers": [{"name": "module-d-evidence", "webhook_configs": [{"url": "http://module-d-alert-receiver:8080/alerts", "send_resolved": True}]}]}
    def metadata(name):
        return {"name": name, "namespace": NS, "labels": {"managed-by": OWNER, "module-d-component": "monitoring", "app.kubernetes.io/part-of": "module-d"}}
    def cm(name, data):
        return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(name), "data": data}
    def workload(name, image, port, command, args, config_name, mount, memory):
        labels = {"app": name, "managed-by": OWNER, "module-d-component": "monitoring", "app.kubernetes.io/part-of": "module-d"}
        container = {"name": name, "image": image, "ports": [{"containerPort": port}],
            "args": args, "resources": {"requests": {"cpu": "100m", "memory": memory}, "limits": {"memory": memory}},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
            "readinessProbe": {"httpGet": {"path": "/health" if port == 8080 else "/-/ready", "port": port}, "periodSeconds": 5},
            "volumeMounts": [{"name": "config", "mountPath": mount, "readOnly": True}, {"name": "data", "mountPath": "/data"}]}
        if command:
            container["command"] = command
        if port == 8080:
            container["env"] = [{"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}]
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata(name),
            "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"app": name}},
                "template": {"metadata": {"labels": labels, "annotations": {"sidecar.istio.io/inject": "false"}},
                    "spec": {"automountServiceAccountToken": False, "enableServiceLinks": False,
                        "securityContext": {"fsGroup": 65534}, "containers": [container],
                        "volumes": [{"name": "config", "configMap": {"name": config_name}}, {"name": "data", "emptyDir": {"sizeLimit": "1Gi"}}]}}}}
        if name in ("module-d-prometheus", "module-d-alertmanager"):
            binary = "/bin/promtool" if port == 9090 else "/bin/amtool"
            check = [binary, "check", "config", "/etc/prometheus/prometheus.yaml"] if port == 9090 else [binary, "check-config", "/etc/alertmanager/alertmanager.yaml"]
            deployment["spec"]["template"]["spec"]["initContainers"] = [{"name": "validate-config", "image": image,
                "command": check, "resources": container["resources"], "securityContext": container["securityContext"],
                "volumeMounts": [{"name": "config", "mountPath": mount, "readOnly": True}]}]
        service = {"apiVersion": "v1", "kind": "Service", "metadata": metadata(name),
            "spec": {"type": "ClusterIP", "selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]}}
        service["metadata"]["annotations"] = {"cloud.google.com/neg": '{"ingress":false}'}
        return [deployment, service]
    objects = [cm("module-d-prometheus", {"prometheus.yaml": json.dumps(prom), "rules.yaml": json.dumps(rules)}),
        cm("module-d-alertmanager", {"alertmanager.yaml": json.dumps(am)}),
        cm("module-d-alert-receiver", {"receiver.py": (HERE / "receiver.py").read_text(encoding="utf-8")})]
    objects += workload("module-d-prometheus", "prom/prometheus:v3.7.2", 9090, None,
        ["--config.file=/etc/prometheus/prometheus.yaml", "--storage.tsdb.path=/data", "--storage.tsdb.retention.time=6h", "--storage.tsdb.retention.size=500MB"],
        "module-d-prometheus", "/etc/prometheus", "512Mi")
    objects += workload("module-d-alertmanager", "prom/alertmanager:v0.28.1", 9093, None,
        ["--config.file=/etc/alertmanager/alertmanager.yaml", "--storage.path=/data", "--cluster.listen-address=", "--log.format=json"],
        "module-d-alertmanager", "/etc/alertmanager", "128Mi")
    objects += workload("module-d-alert-receiver", config["receiver_image"], 8080, ["python", "-u", "/app-monitoring/receiver.py"], [],
        "module-d-alert-receiver", "/app-monitoring", "128Mi")
    objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata("module-d-monitoring-internal"),
        "spec": {"podSelector": {"matchLabels": {"module-d-component": "monitoring"}}, "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}}}]}]}})
    return {"manifest": {"apiVersion": "v1", "kind": "List", "items": objects}, "prometheus": prom,
        "alertmanager": am, "rules": rules, "queries": qs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=NS)
    parser.add_argument("--receiver-image", required=True)
    parser.add_argument("--latency-threshold-ms", type=int, required=True)
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--out", type=Path, required=True)
    for role in ("payments", "data"):
        for field in ("service", "route", "method", "target"):
            parser.add_argument(f"--{role}-{field}", required=True)
    args = parser.parse_args()
    config = {"namespace": args.namespace, "receiver_image": args.receiver_image,
        "latency_threshold_ms": args.latency_threshold_ms, "minimum_samples": args.minimum_samples}
    for role in ("payments", "data"):
        config[role] = {field: getattr(args, role + "_" + field) for field in ("service", "route", "method", "target")}
    files = build(config)
    contract = args.out / "contract.json"
    if contract.exists() and json.loads(contract.read_text())["config"] != config:
        raise SystemExit("Contract changed. Choose a new output directory and declare a new baseline; do not overwrite experiment thresholds.")
    args.out.mkdir(parents=True, exist_ok=True)
    for key, value in files.items():
        (args.out / (key + ".json")).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if not contract.exists():
        contract.write_text(json.dumps({"declared_at": datetime.now(timezone.utc).isoformat(), "config": config,
            "availability_target": 0.995, "latency_target": 0.99, "status": "CONFIGURATION_ONLY_NOT_EXECUTED"}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "status": "RENDERED_NOT_APPLIED", "resources": len(files["manifest"]["items"])}))


if __name__ == "__main__":
    main()
