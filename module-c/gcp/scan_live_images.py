#!/usr/bin/env python3
"""Scan immutable running business images; never infer digests from mutable tags.

Default: inspect the three deployments and save the declared scope, without scanning
or publishing. Add --scan --publish after installing Trivy and configuring registry auth.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess

HERE = Path(__file__).resolve().parent
DEPLOYMENTS = ("orders-service", "payments-service", "data-service")
SIDECARS = frozenset(("istio-proxy", "linkerd-proxy", "vault-agent"))
DIGEST = re.compile(r"^([^\s@]+)@(sha256:[0-9a-f]{64})$")


def command_json(*args):
    result = subprocess.run(list(args), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def repository(reference):
    value = reference.split("@", 1)[0]
    tail = value.rsplit("/", 1)[-1]
    return value.rsplit(":", 1)[0] if ":" in tail else value


def immutable_reference(image_id, desired_image):
    value = re.sub(r"^(?:docker-pullable|docker|containerd)://", "", image_id)
    match = DIGEST.fullmatch(value)
    if not match:
        raise ValueError("Pod imageID is not a registry reference with @sha256; refusing to guess a remote manifest digest")
    if repository(desired_image) != match.group(1):
        raise ValueError("Pod imageID repository differs from its declared image repository")
    if "@" in desired_image and desired_image != value:
        raise ValueError("Declared immutable digest differs from running imageID")
    return value


def derive_inventory(deployments, replica_sets, pods, names=DEPLOYMENTS):
    by_name = {item["metadata"]["name"]: item for item in deployments}
    if set(names) - set(by_name):
        raise ValueError("Missing business deployments: " + ", ".join(sorted(set(names) - set(by_name))))
    inventory = []
    for name in names:
        deployment = by_name[name]
        metadata, spec, status = deployment["metadata"], deployment["spec"], deployment.get("status", {})
        desired = spec.get("replicas", 1)
        if desired < 1 or status.get("observedGeneration", 0) < metadata.get("generation", 1):
            raise ValueError(f"{name}: no active, observed deployment generation")
        if any(status.get(field, 0) != desired for field in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")):
            raise ValueError(f"{name}: rollout is not stable and fully ready; wait before scanning")
        containers = {c["name"]: c["image"] for c in spec["template"]["spec"]["containers"] if c["name"] not in SIDECARS}
        if not containers:
            raise ValueError(f"{name}: no declared business containers")
        rs_uids = {rs["metadata"]["uid"] for rs in replica_sets if any(
            owner.get("kind") == "Deployment" and owner.get("uid") == metadata["uid"] and owner.get("controller", False)
            for owner in rs["metadata"].get("ownerReferences", []))}
        selected = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp") and any(
            owner.get("kind") == "ReplicaSet" and owner.get("uid") in rs_uids and owner.get("controller", False)
            for owner in pod["metadata"].get("ownerReferences", []))]
        if len(selected) != desired:
            raise ValueError(f"{name}: expected {desired} active owned pods, observed {len(selected)}")
        resolved = {container: set() for container in containers}
        for pod in selected:
            pod_status = pod.get("status", {})
            if pod_status.get("phase") != "Running" or not any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod_status.get("conditions", [])):
                raise ValueError(f"{name}: an owned pod is not Ready/Running")
            declared = {c["name"]: c["image"] for c in pod["spec"]["containers"]}
            statuses = {c["name"]: c for c in pod_status.get("containerStatuses", [])}
            for container, desired_image in containers.items():
                state = statuses.get(container, {})
                if declared.get(container) != desired_image or not state.get("ready") or "running" not in state.get("state", {}):
                    raise ValueError(f"{name}/{container}: pod image/spec mismatch or container not running and ready")
                resolved[container].add(immutable_reference(state.get("imageID", ""), desired_image))
        if any(len(images) != 1 for images in resolved.values()):
            raise ValueError(f"{name}: replicas run different digests; refusing a partial or ambiguous scope")
        inventory.append({"deployment": name, "uid": metadata["uid"], "generation": metadata["generation"],
            "replicas": desired, "containers": [{"name": container, "declared_image": containers[container],
                "immutable_image": next(iter(images))} for container, images in sorted(resolved.items())]})
    return inventory


def discover(namespace):
    deployments = command_json("kubectl", "-n", namespace, "get", "deployments", *DEPLOYMENTS, "-o", "json")["items"]
    replica_sets = command_json("kubectl", "-n", namespace, "get", "replicasets", "-o", "json")["items"]
    pods = command_json("kubectl", "-n", namespace, "get", "pods", "-o", "json")["items"]
    return derive_inventory(deployments, replica_sets, pods)


def scanner_module():
    path = HERE.parent / "security" / "cve_snapshot.py"
    spec = importlib.util.spec_from_file_location("module_c_cve_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=HERE / ".state" / "plan.json")
    parser.add_argument("--project", default="crafty-dynamics-239114")
    parser.add_argument("--scanner", default="trivy")
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and not args.scan:
        parser.error("--publish requires --scan; this helper publishes only a fresh complete scan")
    config = json.loads(args.plan.read_text(encoding="utf-8"))["config"]
    if args.project != config["project"]:
        raise ValueError("Requested project differs from the discovered plan")
    expected_context = f"gke_{args.project}_{config['location']}_{config['cluster']}"
    context = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, check=True).stdout.strip()
    if context != expected_context:
        raise ValueError("kubectl context differs from this plan; run gcloud container clusters get-credentials for the discovered cluster first")
    inventory = discover(config["namespace"])
    images = sorted({c["immutable_image"] for deployment in inventory for c in deployment["containers"]})
    now = datetime.now(timezone.utc)
    output = HERE / ".state" / ("cve-scan-" + now.strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    evidence = {"observed_at": now.isoformat(), "project": args.project, "cluster": config["cluster"],
        "namespace": config["namespace"], "scope": "Only application containers of orders-service, payments-service and data-service; excludes sidecars, nodes and cluster add-ons",
        "deployments": inventory, "unique_immutable_images": images}
    (output / "live-inventory.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "INVENTORIED", "output": str(output), "deployments": list(DEPLOYMENTS), "images": images}), flush=True)
    if args.scan:
        scanner = scanner_module()
        snapshot_path = scanner.run_scans(images, output, scanner=args.scanner)
        snapshot = scanner.load_verified_snapshot(snapshot_path)
        if discover(config["namespace"]) != inventory:
            raise ValueError("Deployment inventory changed during scan; reports retained, no metrics published. Rescan the stable scope")
        if args.publish:
            scanner.publish_gcp(snapshot, args.project)
        receipt = {"status": "PUBLISHED" if args.publish else "SCANNED", "snapshot": str(snapshot_path),
            "project": args.project, "completed_at": snapshot["completed_at"], "counts": snapshot["counts"],
            "unique_cves": snapshot["unique_cves"], "non_cve_advisories": snapshot["non_cve_advisories"],
            "scope": evidence["scope"], "unique_images": len(images)}
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        raise SystemExit(f"FAILED: {reason}. No replacement zero snapshot was published.") from None
