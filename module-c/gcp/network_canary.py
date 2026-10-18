#!/usr/bin/env python3
"""Bounded TCP4444 canary within the existing sandbox namespace; auto-cleanup."""
import argparse
import json
import pathlib
import subprocess
import time

NAME = "module-c-network-canary"
REQUEST_COUNT = 240
RESPONSE_BYTES = 64 * 1024


def kubectl(namespace, *args, body=None):
    process = subprocess.run(["kubectl", "-n", namespace, *args],
        input=json.dumps(body) if body is not None else None, text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError(process.stderr.strip())
    return process.stdout


def manifest(namespace, image, port):
    labels = {"app": NAME, "managed-by": "otel-module-c"}
    container = {"name": "canary", "image": image,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "128Mi"}},
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"}}}
    server_code = ("from http.server import BaseHTTPRequestHandler,HTTPServer\n"
        f"BODY=b'C'*{RESPONSE_BYTES}\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type','application/octet-stream')\n"
        "        self.send_header('Content-Length',str(len(BODY)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(BODY)\n"
        "    def log_message(self,*args): pass\n"
        f"HTTPServer(('0.0.0.0',{port}),Handler).serve_forever()\n")
    server = dict(container, command=["python", "-c", server_code],
        ports=[{"containerPort": port}], readinessProbe={"tcpSocket": {"port": port}, "periodSeconds": 2})
    client_code = ("import time,urllib.request,json\n"
        "from datetime import datetime,timezone\n"
        "def utc(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')\n"
        f"url='http://{NAME}:{port}/'\n"
        "received=0\n"
        "print(json.dumps({'event':'canary_requests_started','timestamp':utc()}),flush=True)\n"
        f"for i in range({REQUEST_COUNT}):\n"
        f"    with urllib.request.urlopen(url,timeout=5) as r: data=r.read({RESPONSE_BYTES + 1})\n"
        f"    if len(data)!={RESPONSE_BYTES}: raise RuntimeError('Unexpected response size; stopped')\n"
        "    received+=len(data)\n"
        "    time.sleep(0.5)\n"
        f"print(json.dumps({{'event':'canary_requests_completed','timestamp':utc(),'requests':{REQUEST_COUNT},'received_payload_bytes':received}}),flush=True)\n")
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": NAME}}, "template": {
            "metadata": {"labels": labels, "annotations": {"sidecar.istio.io/inject": "false"}},
            "spec": {"automountServiceAccountToken": False, "containers": [server]}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
        "spec": {"type": "ClusterIP", "selector": {"app": NAME}, "ports": [{"port": port, "targetPort": port}]}}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": NAME, "namespace": namespace, "labels": labels},
        "spec": {"ttlSecondsAfterFinished": 600, "activeDeadlineSeconds": 240, "backoffLimit": 0,
            "template": {"metadata": {"labels": {"app": NAME + "-client", "managed-by": "otel-module-c"},
                                      "annotations": {"sidecar.istio.io/inject": "false"}},
                "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never", "containers": [dict(container, command=["python", "-c", client_code])]}}}}
    return deployment, service, job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=pathlib.Path, default=pathlib.Path(__file__).with_name(".state") / "plan.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.plan.read_text(encoding="utf-8"))["config"]
    namespace = config["namespace"]
    if not args.apply:
        print(f"Plan: create {NAME} Deployment/ClusterIP/Job in {config['project']}/{config['cluster']}/{namespace}; "
              f"{REQUEST_COUNT} TCP requests to port {config['unexpected_port']}, {RESPONSE_BYTES} response bytes each "
              f"({REQUEST_COUNT * RESPONSE_BYTES} total payload bytes), then delete only these resources. Add --apply to run.")
        return
    subprocess.run(["gcloud", "container", "clusters", "get-credentials", config["cluster"], "--location", config["location"],
                    "--project", config["project"]], check=True)
    # Existing resources with these names are never overwritten.
    for kind in ("deployment", "service", "job"):
        if kubectl(namespace, "get", kind, NAME, "--ignore-not-found", "-o", "name").strip():
            raise RuntimeError(f"Existing {kind}/{NAME}; inspect/clean it manually before running.")
    orders = json.loads(kubectl(namespace, "get", "deployment", "orders-service", "-o", "json"))
    image = orders["spec"]["template"]["spec"]["containers"][0]["image"]
    deployment, service, job = manifest(namespace, image, config["unexpected_port"])
    created = []
    started = time.time()
    cleanup_errors = []
    try:
        for kind, resource in (("deployment", deployment), ("service", service)):
            kubectl(namespace, "create", "-f", "-", body=resource)
            created.append(kind)
        kubectl(namespace, "rollout", "status", "deployment/" + NAME, "--timeout=300s")
        kubectl(namespace, "create", "-f", "-", body=job)
        created.append("job")
        kubectl(namespace, "wait", "--for=condition=complete", "job/" + NAME, "--timeout=300s")
        job_result = json.loads(kubectl(namespace, "get", "job", NAME, "-o", "json"))
        print(kubectl(namespace, "logs", "job/" + NAME))
        print(json.dumps({"provisioning_start_unix": started, "job_started_at": job_result.get("status", {}).get("startTime"),
                         "end_unix": time.time(), "requests": REQUEST_COUNT, "expected_payload_bytes": REQUEST_COUNT * RESPONSE_BYTES,
                         "note": "Traffic generated. Verify actual Flow Logs and incident separately."}))
    finally:
        for kind in reversed(created):
            try:
                kubectl(namespace, "delete", kind, NAME, "--ignore-not-found", "--wait=false")
            except RuntimeError as exc:
                cleanup_errors.append(f"{kind}/{NAME}: {exc}")
        if cleanup_errors:
            raise RuntimeError("Cleanup incomplete; inspect only these owned resources: " + "; ".join(cleanup_errors))


if __name__ == "__main__":
    main()
