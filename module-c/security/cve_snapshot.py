"""Scan the declared image scope and publish a verified CVE snapshot to GCP.

Only a complete Trivy run for every declared image may publish zero values.
No credentials, scanner output, or gcloud access tokens are printed.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
CVE_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,}$")


def utc_now():
    return datetime.now(timezone.utc)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_reports(paths):
    """Distinct CVE IDs across scope; highest observed severity wins.

    A CVE present in three packages/images counts once. Findings with an
    available fixed version still count until the installed artifact is fixed.
    Non-CVE advisory IDs are tracked separately, not relabeled as CVEs.
    """
    if not paths:
        raise ValueError("At least one complete Trivy report is required")
    cves = {}
    advisory_ids = set()
    for path in paths:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if report.get("SchemaVersion") != 2 or not isinstance(report.get("Results"), list):
            raise ValueError("Expected a complete Trivy schema v2 report with a Results array")
        if report.get("Error") or report.get("Errors"):
            raise ValueError("Report contains scanner errors; refusing a partial snapshot")
        for result in report["Results"]:
            if not isinstance(result, dict):
                raise ValueError("Invalid Trivy result")
            if result.get("Error") or result.get("Errors"):
                raise ValueError("Result contains scanner errors; refusing a partial snapshot")
            vulnerabilities = result.get("Vulnerabilities")
            vulnerabilities = [] if vulnerabilities is None else vulnerabilities
            if not isinstance(vulnerabilities, list):
                raise ValueError("Invalid Trivy vulnerability list")
            for vulnerability in vulnerabilities:
                if not isinstance(vulnerability, dict):
                    raise ValueError("Invalid Trivy vulnerability record")
                identifier = vulnerability.get("VulnerabilityID")
                severity = vulnerability.get("Severity")
                if not isinstance(identifier, str) or not identifier or severity not in SEVERITIES:
                    raise ValueError("Incomplete vulnerability ID/severity; refusing misleading counts")
                if not CVE_PATTERN.fullmatch(identifier):
                    advisory_ids.add(identifier)
                    continue
                if identifier not in cves or SEVERITIES.index(severity) > SEVERITIES.index(cves[identifier]):
                    cves[identifier] = severity
    counts = Counter(cves.values())
    return {
        "counts": {severity: counts[severity] for severity in SEVERITIES},
        "unique_cves": len(cves),
        "non_cve_advisories": len(advisory_ids),
        "cves": dict(sorted(cves.items())),
    }


def run_scans(images, output_directory, scanner="trivy"):
    """A nonzero scanner exit aborts the whole scope before any publication."""
    if not images or len(set(images)) != len(images):
        raise ValueError("Provide a nonempty, unique image scope")
    if any(image.startswith("-") or not image.strip() for image in images):
        raise ValueError("Invalid image reference")
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # A new output directory keeps previous hash-verified reports intact.
    if (output / "snapshot.json").exists() or list(output.glob("trivy-*.json")):
        raise ValueError("Use a fresh output directory for each complete snapshot")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("TRIVY_")}
    reports = []
    with tempfile.TemporaryDirectory(prefix="module-c-trivy-") as temp:
        empty_ignore = Path(temp) / "empty.trivyignore"
        empty_ignore.write_text("", encoding="utf-8")
        empty_config = Path(temp) / "empty.yaml"
        empty_config.write_text("{}\n", encoding="utf-8")
        version = subprocess.run(
            [scanner, "--version"], capture_output=True, text=True, check=True, env=environment,
        ).stdout.strip().splitlines()[0]
        started = utc_now().isoformat()
        for index, image in enumerate(images, 1):
            report_path = output / f"trivy-{index}.json"
            command = [
                scanner, "image", "--config", str(empty_config), "--scanners", "vuln",
                "--severity", ",".join(SEVERITIES), "--ignore-unfixed=false",
                "--ignorefile", str(empty_ignore), "--exit-code", "0", "--format", "json",
                "--timeout", "15m", "--output", str(report_path), image,
            ]
            result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=960)
            if result.returncode != 0:
                raise RuntimeError(f"Trivy failed for scope image {index}; no metrics published")
            # Validate even if the scanner returned success; an empty file is not zero CVEs.
            parse_reports([report_path])
            reports.append({"image": image, "file": report_path.name, "sha256": file_hash(report_path)})
    aggregate = parse_reports([output / report["file"] for report in reports])
    snapshot = {
        "schema_version": 1,
        "status": "COMPLETE",
        "scanner": version,
        "started_at": started,
        "completed_at": utc_now().isoformat(),
        "scope_definition": "All provided images, all severities, unfixed included; unique CVE ID, maximum severity across scope",
        "scope": reports,
        **aggregate,
    }
    snapshot_path = output / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return snapshot_path


def load_verified_snapshot(path, now=None, max_age_seconds=86400):
    path = Path(path).resolve()
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("schema_version") != 1 or snapshot.get("status") != "COMPLETE":
        raise ValueError("Only COMPLETE schema v1 snapshots can be published")
    if not isinstance(snapshot.get("scope"), list) or not snapshot["scope"]:
        raise ValueError("Snapshot scope cannot be empty")
    completed = datetime.fromisoformat(snapshot["completed_at"])
    if completed.tzinfo is None:
        raise ValueError("Snapshot time must include timezone")
    age = ((now or utc_now()) - completed).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise ValueError("Snapshot is stale or has a future timestamp; scan the scope again")
    report_paths = []
    for item in snapshot["scope"]:
        report_path = (path.parent / item["file"]).resolve()
        if report_path.parent != path.parent or file_hash(report_path) != item["sha256"]:
            raise ValueError("Snapshot report path/hash verification failed")
        report_paths.append(report_path)
    aggregate = parse_reports(report_paths)
    if any(snapshot.get(key) != value for key, value in aggregate.items()):
        raise ValueError("Snapshot counts do not match the complete source reports")
    return snapshot


def gcp_time_series(snapshot, project_id, timestamp=None):
    write_time = (timestamp or utc_now()).isoformat().replace("+00:00", "Z")
    resource = {"type": "global", "labels": {"project_id": project_id}}
    series = []
    for severity in SEVERITIES:
        series.append({
            "metric": {"type": "custom.googleapis.com/security/active_cves", "labels": {"severity": severity}},
            "resource": resource, "metricKind": "GAUGE", "valueType": "INT64",
            "points": [{"interval": {"endTime": write_time}, "value": {"int64Value": str(snapshot["counts"][severity])}}],
        })
    scan_time = int(datetime.fromisoformat(snapshot["completed_at"]).timestamp())
    series.append({
        "metric": {"type": "custom.googleapis.com/security/cve_snapshot_timestamp"},
        "resource": resource, "metricKind": "GAUGE", "valueType": "INT64",
        "points": [{"interval": {"endTime": write_time}, "value": {"int64Value": str(scan_time)}}],
    })
    return series


def publish_gcp(snapshot, project_id):
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project_id):
        raise ValueError("Provide a valid GCP project ID")
    result = subprocess.run(
        [shutil.which("gcloud") or "gcloud", "auth", "print-access-token"], capture_output=True, text=True, check=True,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud did not provide an access token")
    request = urllib.request.Request(
        f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries",
        data=json.dumps({"timeSeries": gcp_time_series(snapshot, project_id)}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        # Do not dump requests, credentials or potentially sensitive API errors.
        raise RuntimeError(f"GCP rejected the metrics publication (HTTP {exc.code})") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Run every image scan; a failure prevents snapshot publication")
    scan.add_argument("--image", action="append", required=True, help="Repeat for every image in the complete sandbox scope")
    scan.add_argument("--output-dir", required=True)
    scan.add_argument("--scanner", default="trivy")
    scan.add_argument("--project", help="If set, publish after the successful complete scan")
    publish = commands.add_parser("publish", help="Verify source report hashes and publish a recent COMPLETE snapshot")
    publish.add_argument("--snapshot", required=True)
    publish.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        path = run_scans(args.image, args.output_dir, args.scanner) if args.command == "scan" else Path(args.snapshot)
        snapshot = load_verified_snapshot(path)
        if args.project:
            publish_gcp(snapshot, args.project)
        print(json.dumps({
            "status": "PUBLISHED" if args.project else "SCANNED",
            "snapshot": str(path), "completed_at": snapshot["completed_at"],
            "unique_cves": snapshot["unique_cves"], "counts": snapshot["counts"],
            "non_cve_advisories": snapshot["non_cve_advisories"],
        }))
    except (ValueError, KeyError, IndexError, OSError, RuntimeError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        # Commands can contain registry information; intentionally keep errors concise.
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        raise SystemExit(f"FAILED: {reason}. Snapshot counts were not replaced with zero.") from None


if __name__ == "__main__":
    main()
