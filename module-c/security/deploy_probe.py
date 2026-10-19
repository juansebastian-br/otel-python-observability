"""Deploy only the independently owned Module C authentication sandbox.

Secrets go to kubectl on stdin and are never printed or written to disk.
Existing managed keys are preserved. Unmanaged objects cause a preflight failure.
"""
import argparse
import base64
import copy
import json
import re
import secrets
import shutil
import subprocess

NAME = "module-c-auth-probe"
OWNER = "otel-module-c"
LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/managed-by": OWNER}


def execute(command, body=None):
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        # Kubernetes error messages can echo invalid Secret objects; do not dump stderr.
        raise RuntimeError(f"{command[0].split('/')[-1]} operation failed (exit {result.returncode}); no credential output retained")
    return result.stdout


def validate_image(image, registry, project):
    # Exact Artifact Registry repository, not an unrestricted substring match.
    pattern = rf"[a-z][a-z0-9-]*-docker\.pkg\.dev/{re.escape(project)}/[a-z][a-z0-9._-]*"
    if not re.fullmatch(pattern, registry):
        raise ValueError("--registry must be an exact Artifact Registry repository in --project")
    suffix = rf"/{NAME}(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}|@sha256:[a-f0-9]{{64}})"
    if not re.fullmatch(re.escape(registry) + suffix, image):
        raise ValueError("Image must be module-c-auth-probe with a tag or digest under the exact --registry repository")


def resolve_context(args):
    if args.context:
        if args.cluster or args.location:
            raise ValueError("Use --context or --cluster with --location, not both")
        return args.context
    if not args.cluster or not args.location:
        raise ValueError("Specify --context, or both --cluster and --location with --project")
    execute([shutil.which("gcloud") or "gcloud", "container", "clusters", "get-credentials", args.cluster,
             "--location", args.location, "--project", args.project, "--quiet"])
    return f"gke_{args.project}_{args.location}_{args.cluster}"


def manifests(namespace, image, endpoint):
    metadata = {"name": NAME, "namespace": namespace, "labels": dict(LABELS)}
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": copy.deepcopy(metadata),
        "spec": {"replicas": 1, "revisionHistoryLimit": 2,
                 "selector": {"matchLabels": {"app.kubernetes.io/name": NAME}},
                 "template": {
                     "metadata": {"labels": dict(LABELS), "annotations": {"sidecar.istio.io/inject": "false"}},
                     "spec": {"automountServiceAccountToken": False,
                              "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                                  "seccompProfile": {"type": "RuntimeDefault"}},
                              "containers": [{"name": NAME, "image": image, "imagePullPolicy": "Always",
                                  "ports": [{"name": "http", "containerPort": 8080}],
                                  "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                "limits": {"cpu": "500m", "memory": "256Mi"}},
                                  "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                      "capabilities": {"drop": ["ALL"]}},
                                  "env": [
                                      {"name": "SECURITY_AUTH_ENABLED", "value": "true"},
                                      {"name": "DEPLOYMENT_ENVIRONMENT", "value": "sandbox"},
                                      {"name": "OTEL_SERVICE_NAME", "value": NAME},
                                      {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": endpoint},
                                      {"name": "OTEL_EXPORTER_OTLP_INSECURE", "value": "true"},
                                      {"name": "SECURITY_API_KEY", "valueFrom": {"secretKeyRef": {"name": NAME, "key": "API_KEY"}}},
                                  ],
                                  "readinessProbe": {"httpGet": {"path": "/health", "port": "http"},
                                                     "initialDelaySeconds": 3, "periodSeconds": 5},
                                  "livenessProbe": {"httpGet": {"path": "/health", "port": "http"},
                                                    "initialDelaySeconds": 10, "periodSeconds": 10},
                              }]},
                 }},
    }
    service = {"apiVersion": "v1", "kind": "Service", "metadata": copy.deepcopy(metadata),
               "spec": {"type": "ClusterIP", "selector": {"app.kubernetes.io/name": NAME},
                        "ports": [{"name": "http", "port": 8080, "targetPort": "http", "protocol": "TCP"}]}}
    return deployment, service


def check_owned(resource):
    if resource is None:
        return
    labels = resource.get("metadata", {}).get("labels", {})
    if any(labels.get(key) != value for key, value in LABELS.items()):
        raise ValueError(f"Refusing to overwrite unmanaged {resource.get('kind', 'resource')} {NAME}")


def replacement(desired, existing):
    if not existing:
        return desired
    result = copy.deepcopy(desired)
    result["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    result["metadata"]["uid"] = existing["metadata"]["uid"]
    if desired["kind"] == "Service":
        # Preserve API-assigned immutable IP details on repeat deployments.
        result["spec"] = {**copy.deepcopy(existing["spec"]), **result["spec"]}
    return result


def deploy(args):
    validate_image(args.image, args.registry, args.project)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace):
        raise ValueError("Invalid namespace")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*:[0-9]{1,5}", args.otlp_endpoint):
        raise ValueError("Use a cluster-local OTLP endpoint in hostname:port form")
    context = resolve_context(args)
    kubectl = [shutil.which("kubectl") or "kubectl", "--context", context]
    execute([*kubectl, "get", "namespace", args.namespace, "-o", "name"])
    scoped = [*kubectl, "--namespace", args.namespace]
    existing = {}
    for kind in ("Secret", "Deployment", "Service"):
        output = execute([*scoped, "get", kind.lower(), NAME, "--ignore-not-found=true", "-o", "json"])
        existing[kind] = json.loads(output) if output.strip() else None
        check_owned(existing[kind])
    secret = existing["Secret"]
    if secret:
        try:
            credential = base64.b64decode(secret.get("data", {}).get("API_KEY", ""), validate=True)
        except ValueError:
            raise ValueError("Existing managed Secret has an invalid API_KEY encoding") from None
        if len(credential) < 24:
            raise ValueError("Existing managed Secret API_KEY is missing or too short; refusing silent rotation")
    if existing["Service"] and existing["Service"]["spec"].get("type", "ClusterIP") != "ClusterIP":
        raise ValueError("Existing managed service is not ClusterIP; refusing to change its exposure")
    deployment, service = manifests(args.namespace, args.image, args.otlp_endpoint)
    if existing["Deployment"] and existing["Deployment"]["spec"].get("selector") != deployment["spec"]["selector"]:
        raise ValueError("Existing managed deployment has a different selector; refusing to replace it")
    # All ownership and integrity checks above precede the first cluster mutation.
    if not secret:
        new_secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                      "metadata": {"name": NAME, "namespace": args.namespace, "labels": dict(LABELS)},
                      "stringData": {"API_KEY": secrets.token_urlsafe(48)}}
        execute([*scoped, "create", "-f", "-"], new_secret)
    for resource in (deployment, service):
        previous = existing[resource["kind"]]
        # create refuses a concurrently introduced name; replace uses UID and
        # resourceVersion to reject races and stale ownership observations.
        execute([*scoped, "replace" if previous else "create", "-f", "-"], replacement(resource, previous))
    execute([*scoped, "rollout", "status", f"deployment/{NAME}", "--timeout=180s"])
    return {"status": "READY", "context": context, "namespace": args.namespace, "service": NAME,
            "image": args.image, "port": 8080, "secret": "preserved" if secret else "created"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--registry", required=True, help="Exact Artifact Registry host/project/repository prefix")
    parser.add_argument("--project", default="crafty-dynamics-239114")
    parser.add_argument("--namespace", default="observability-lab")
    parser.add_argument("--context")
    parser.add_argument("--cluster")
    parser.add_argument("--location")
    parser.add_argument("--otlp-endpoint", default="otel-collector:4317")
    args = parser.parse_args()
    try:
        print(json.dumps(deploy(args)))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Deployment stopped: {exc}") from None


if __name__ == "__main__":
    main()
