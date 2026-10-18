#!/usr/bin/env python3
"""Optional subnet-only Network Management diagnostic; never changes live metrics.

plan/status/capture do not create resources. create and cleanup affect only the
named configuration with proven ownership and exact subnet from the saved plan.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import urllib.parse

from deploy import OWNER, api, gcloud, save

HERE = Path(__file__).resolve().parent
NAME = "module-c-subnet-diagnostic"
BASE = "https://networkmanagement.googleapis.com/v1/"


def configuration(config):
    return {"subnet": f"projects/{config['project']}/regions/{config['region']}/subnetworks/{config['subnet']}",
        "description": "Module C subnet-only diagnostic; separate log family, never combined with legacy metrics",
        "state": "ENABLED", "aggregationInterval": "INTERVAL_5_SEC", "flowSampling": 1.0,
        "metadata": "INCLUDE_ALL_METADATA", "filterExpr": "true",
        # Full enum observed accepted by the live API; short ENABLED is invalid here.
        "connectionLogging": "CONNECTION_LOGGING_ENABLED", "labels": {"managed_by": OWNER}}


def assert_owned(existing, desired, receipts=()):
    if existing.get("subnet") != desired["subnet"]:
        raise ValueError("Configuration targets a different subnet; refusing mutation")
    if existing.get("labels", {}).get("managed_by") == OWNER:
        return
    # Some live API versions omit labels, even when submitted during creation.
    # Accept a saved successful creation response only if all identifying fields
    # match exactly. Never claim ownership from the resource name alone.
    for receipt in receipts:
        if "response" in receipt:
            if not receipt.get("done") or receipt.get("error"):
                continue
            receipt = receipt["response"]
        if isinstance(receipt, dict) and all(
            isinstance(existing.get(key), str) and bool(existing[key]) and existing[key] == receipt.get(key)
            for key in ("name", "subnet", "createTime", "description")
        ):
            return
    raise ValueError("Configuration has no matching owner label or exact saved creation receipt; refusing mutation")


def wait_operation(operation, parent, output, timeout=300):
    start = time.monotonic()
    while not operation.get("done"):
        if time.monotonic() - start > timeout:
            raise RuntimeError("Operation still pending; capture status before retrying creation or cleanup")
        name = operation["name"]
        if not name.startswith("projects/"):
            name = parent + "/" + name
        time.sleep(5)
        operation = api("GET", BASE + name)
        save(output / "operation.json", operation)
    if operation.get("error"):
        raise RuntimeError("Network Management operation failed: " + json.dumps(operation["error"]))
    return operation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "status", "capture", "create", "cleanup"))
    parser.add_argument("--plan", type=Path, default=HERE / ".state" / "plan.json")
    parser.add_argument("--creation-receipt", type=Path,
                        help="Successful creation operation JSON or saved configuration receipt; required fields must match live config")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    config = plan["config"]
    desired = configuration(config)
    receipt_paths = [args.creation_receipt] if args.creation_receipt else [
        HERE / ".state" / "flow-diagnostic-owned.json", HERE / ".state" / "network-diagnostic-operation.json"]
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in receipt_paths if path.exists()]
    parent = f"projects/{config['project']}/locations/global"
    collection = BASE + parent + "/vpcFlowLogsConfigs"
    url = collection + "/" + NAME
    output = HERE / ".state" / ("flow-diagnostic-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    if args.action == "plan":
        save(output / "proposed-config.json", desired)
        print(json.dumps({"resource": url, "body": desired, "note": "Read-only proposal; no resource created"}, indent=2))
        return
    if args.action == "create":
        enabled = gcloud("services", "list", "--enabled", "--project", config["project"])
        if "networkmanagement.googleapis.com" not in {s.get("config", {}).get("name") for s in enabled}:
            gcloud("services", "enable", "networkmanagement.googleapis.com", "--project", config["project"])
    existing = api("GET", url, missing_ok=True)
    save(output / "configuration-before.json", existing)
    if args.action == "create":
        if existing:
            assert_owned(existing, desired, receipts)
            for field in ("state", "aggregationInterval", "flowSampling", "metadata", "connectionLogging"):
                if existing.get(field) != desired[field]:
                    raise ValueError(f"Existing owned configuration differs at {field}; no settings overwritten")
            print("Owned identical configuration already exists; no duplicate created.")
        else:
            operation = api("POST", collection + "?vpcFlowLogsConfigId=" + NAME, desired)
            save(output / "operation.json", operation)
            wait_operation(operation, parent, output)
        current = api("GET", url)
        save(output / "configuration-after.json", current)
        save(HERE / ".state" / "flow-diagnostic-owned.json", current)
        print(json.dumps({"status": "CONFIGURED", "resource": current["name"], "evidence": str(output),
            "note": "No telemetry claimed. Existing metric filters remain Compute-only to avoid double counting."}))
    elif args.action == "cleanup":
        if config.get("flow_backend") == "network-management" and config.get("flow_config_name") == NAME:
            raise ValueError("This configuration is the selected main Flow Logs backend; migrate the main plan before cleanup")
        if existing:
            assert_owned(existing, desired, receipts)
            operation = api("DELETE", url)
            save(output / "operation.json", operation)
            wait_operation(operation, parent, output)
            if api("GET", url, missing_ok=True) is not None:
                raise RuntimeError("Deletion operation completed but config still visible; verify status")
        print(json.dumps({"status": "ABSENT", "resource": url, "evidence": str(output)}))
    else:
        effective = api("GET", collection + ":showEffectiveFlowLogsConfigs?" + urllib.parse.urlencode({"resource": desired["subnet"]}))
        save(output / "effective-configurations.json", effective)
        result = {"status": "INSPECTED", "configuration_exists": existing is not None, "evidence": str(output)}
        if args.action == "capture":
            start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            query = ('log_id("networkmanagement.googleapis.com/vpc_flows") AND resource.type="vpc_flow_logs_config" '
                     f'AND resource.labels.name="{NAME}" AND timestamp >= "{start}"')
            logs = api("POST", "https://logging.googleapis.com/v2/entries:list", {
                "resourceNames": ["projects/" + config["project"]], "filter": query, "pageSize": 100, "orderBy": "timestamp desc"})
            save(output / "diagnostic-flow-logs.json", logs)
            result.update({"observed_records": len(logs.get("entries", [])), "truncated": bool(logs.get("nextPageToken")),
                           "query": query})
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
