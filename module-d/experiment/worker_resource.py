"""Pure Kubernetes resources for an isolated in-cluster load worker."""
import ast
import hashlib
import json
import re

NAMESPACE = "observability-chaos-d"
OWNER = "otel-module-d"
COMPONENT = "load-generator"
CONTAINER = "module-d-worker"
SOURCE_NAMES = {"__init__.py", "runner.py", "analyze.py", "worker.py"}
ID_PATTERN = re.compile(r"d-(?:latency|error)-[a-f0-9]{32}")
IMAGE_PATTERN = re.compile(r"[^\s]+@sha256:[0-9a-f]{64}")


def labels(experiment_id):
    return {"managed-by": OWNER, "app.kubernetes.io/part-of": "module-d",
            "module-d-component": COMPONENT, "app": experiment_id, "module-d-experiment": experiment_id}


def validate_identity(metadata, experiment_id):
    if metadata.get("namespace") != NAMESPACE:
        raise ValueError("Worker must belong to the dedicated Module D namespace")
    actual = metadata.get("labels", {})
    if any(actual.get(key) != value for key, value in labels(experiment_id).items()):
        raise ValueError("Worker ownership labels do not match the experiment")


def build_worker_manifest(experiment_id, image_digest, source_files, worker_config):
    if not ID_PATTERN.fullmatch(experiment_id):
        raise ValueError("Worker ID must be d-latency- or d-error- followed by 32 lowercase hexadecimal characters")
    if not IMAGE_PATTERN.fullmatch(image_digest):
        raise ValueError("Worker must reuse the approved Python app image by immutable digest")
    if set(source_files) != SOURCE_NAMES or any(not isinstance(value, str) for value in source_files.values()):
        raise ValueError("Worker package must contain exactly the four reviewed Python source files")
    for filename, value in source_files.items():
        ast.parse(value, filename=filename)
    if not isinstance(worker_config, dict) or worker_config.get("experiment_id") != experiment_id:
        raise ValueError("Worker configuration must identify this exact experiment")
    if worker_config.get("namespace", NAMESPACE) != NAMESPACE:
        raise ValueError("Cross-namespace worker configuration is forbidden")
    encoded = json.dumps(worker_config, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > 65536 or sum(len(value.encode()) for value in source_files.values()) > 900000:
        raise ValueError("Worker configuration/package exceeds its bounded ConfigMap size")
    def metadata(name):
        return {"name": name, "namespace": NAMESPACE, "labels": labels(experiment_id)}
    source_name, config_name = experiment_id + "-source", experiment_id + "-config"
    objects = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(source_name), "data": source_files},
               {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata(config_name), "data": {"worker.json": encoded}}]
    checksum = hashlib.sha256(json.dumps({"source": source_files, "config": worker_config}, sort_keys=True).encode()).hexdigest()
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": metadata(experiment_id),
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 2400, "ttlSecondsAfterFinished": 3600,
            "parallelism": 1, "completions": 1,
            "template": {"metadata": {"labels": labels(experiment_id), "annotations": {
                "sidecar.istio.io/inject": "false", "module-d/config-sha256": checksum}},
                "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                    "enableServiceLinks": False, "securityContext": {"fsGroup": 1000},
                    "containers": [{"name": CONTAINER, "image": image_digest, "workingDir": "/opt/module-d",
                        "command": ["python", "-u", "-m", "experiment.worker"], "args": ["--config", "/config/worker.json", "--out", "/evidence"],
                        "env": [{"name": "PYTHONPATH", "value": "/opt/module-d"},
                            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                            {"name": "DEPLOYMENT_ENVIRONMENT", "value": "sandbox"},
                            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}],
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
                        "resources": {"requests": {"cpu": "100m", "memory": "256Mi", "ephemeral-storage": "256Mi"},
                            "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "256Mi"}},
                        "volumeMounts": [{"name": "source", "mountPath": "/opt/module-d/experiment", "readOnly": True},
                            {"name": "config", "mountPath": "/config", "readOnly": True},
                            {"name": "evidence", "mountPath": "/evidence"}]}],
                    "volumes": [{"name": "source", "configMap": {"name": source_name}},
                        {"name": "config", "configMap": {"name": config_name}},
                        {"name": "evidence", "emptyDir": {"sizeLimit": "256Mi"}}]}}}}
    objects.append(job)
    return {"apiVersion": "v1", "kind": "List", "items": objects}


def validate_ready_worker(job, pods):
    """Return only the ready Pod owned by the immutable expected Job identity."""
    metadata = job.get("metadata", {})
    identifier = metadata.get("name", "")
    if job.get("kind") != "Job" or not ID_PATTERN.fullmatch(identifier) or not metadata.get("uid"):
        raise ValueError("Invalid worker Job identity")
    validate_identity(metadata, identifier)
    containers = job["spec"]["template"]["spec"]["containers"]
    if len(containers) != 1 or containers[0].get("name") != CONTAINER or not IMAGE_PATTERN.fullmatch(containers[0].get("image", "")):
        raise ValueError("Invalid worker container/image")
    image = containers[0]["image"]
    candidates = []
    for pod in pods:
        meta = pod.get("metadata", {})
        if not any(owner.get("kind") == "Job" and owner.get("uid") == metadata["uid"] for owner in meta.get("ownerReferences", [])):
            continue
        validate_identity(meta, identifier)
        if meta.get("deletionTimestamp") or pod.get("status", {}).get("phase") != "Running":
            continue
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions", [])):
            continue
        actual = pod.get("spec", {}).get("containers", [])
        status = pod["status"].get("containerStatuses", [])
        if len(actual) != 1 or actual[0].get("name") != CONTAINER or actual[0].get("image") != image or len(status) != 1:
            raise ValueError("Unexpected worker containers or images")
        running_image = re.sub(r"^[a-zA-Z0-9+.-]+://", "", status[0].get("imageID", ""))
        if status[0].get("name") != CONTAINER or not status[0].get("ready") or running_image != image:
            raise ValueError("Worker running image digest/readiness differs from the declared Job")
        candidates.append({"pod": meta["name"], "pod_uid": meta["uid"], "container": CONTAINER,
                           "image": image, "job": identifier, "job_uid": metadata["uid"]})
    if len(candidates) != 1:
        raise ValueError("Exactly one ready worker Pod is required")
    return candidates[0]
