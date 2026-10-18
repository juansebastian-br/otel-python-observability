"""Local unit checks, not evidence that cloud metrics or a scan ran."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cve_snapshot", ROOT / "module-c/security/cve_snapshot.py")
cve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cve)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime.now(timezone.utc)

    def report(self, name="trivy-1.json", entries=None):
        path = self.root / name
        path.write_text(json.dumps({"SchemaVersion": 2, "Results": [
            {"Target": "sandbox-image", "Vulnerabilities": entries or []}
        ]}), encoding="utf-8")
        return path

    def snapshot(self, report, completed=None):
        result = {"schema_version": 1, "status": "COMPLETE",
                  "completed_at": (completed or self.now).isoformat(),
                  "scope": [{"image": "sandbox-image", "file": report.name, "sha256": cve.file_hash(report)}],
                  **cve.parse_reports([report])}
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return path, result

    def test_unique_cve_uses_highest_severity_across_images(self):
        first = self.report(entries=[
            {"VulnerabilityID": "CVE-2025-12345", "Severity": "LOW"},
            {"VulnerabilityID": "CVE-2025-12345", "Severity": "HIGH"},
            {"VulnerabilityID": "GHSA-test-id", "Severity": "CRITICAL"},
        ])
        second = self.report("trivy-2.json", [
            {"VulnerabilityID": "CVE-2025-12345", "Severity": "CRITICAL"},
            {"VulnerabilityID": "CVE-2025-67890", "Severity": "MEDIUM", "FixedVersion": "2.0"},
        ])
        result = cve.parse_reports([first, second])
        self.assertEqual(result["unique_cves"], 2)
        self.assertEqual(result["counts"], {"UNKNOWN": 0, "LOW": 0, "MEDIUM": 1, "HIGH": 0, "CRITICAL": 1})
        self.assertEqual(result["non_cve_advisories"], 1)

    def test_empty_successful_report_publishes_explicit_zeros(self):
        path, _ = self.snapshot(self.report())
        snapshot = cve.load_verified_snapshot(path, self.now)
        self.assertEqual(sum(snapshot["counts"].values()), 0)
        series = cve.gcp_time_series(snapshot, "sandbox-project", self.now)
        self.assertEqual(len(series), 6)
        self.assertEqual({item["metric"]["labels"]["severity"] for item in series[:5]}, set(cve.SEVERITIES))
        self.assertTrue(all(item["points"][0]["value"]["int64Value"] == "0" for item in series[:5]))
        self.assertEqual(series[-1]["points"][0]["value"]["int64Value"], str(int(self.now.timestamp())))

    def test_no_reports_cannot_be_interpreted_as_zero(self):
        with self.assertRaises(ValueError):
            cve.parse_reports([])

    def test_partial_report_cannot_be_interpreted_as_zero(self):
        path = self.root / "partial.json"
        path.write_text('{"SchemaVersion": 2}', encoding="utf-8")
        with self.assertRaises(ValueError):
            cve.parse_reports([path])

    def test_unknown_severity_cannot_drop_finding(self):
        path = self.report(entries=[{"VulnerabilityID": "CVE-2025-12345", "Severity": "BROKEN"}])
        with self.assertRaises(ValueError):
            cve.parse_reports([path])

    def test_modified_report_hash_rejected(self):
        report = self.report()
        path, _ = self.snapshot(report)
        report.write_text('{"SchemaVersion": 2, "Results": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            cve.load_verified_snapshot(path, self.now)

    def test_modified_counts_rejected(self):
        path, result = self.snapshot(self.report())
        result["counts"]["HIGH"] = 999
        path.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaises(ValueError):
            cve.load_verified_snapshot(path, self.now)

    def test_stale_and_future_snapshots_rejected(self):
        report = self.report()
        for delta in (timedelta(days=-2), timedelta(minutes=5)):
            path, _ = self.snapshot(report, self.now + delta)
            with self.assertRaises(ValueError):
                cve.load_verified_snapshot(path, self.now)

    def test_republication_preserves_actual_scan_time(self):
        completed = self.now - timedelta(minutes=20)
        path, _ = self.snapshot(self.report(), completed)
        series = cve.gcp_time_series(cve.load_verified_snapshot(path, self.now), "sandbox-project", self.now)
        self.assertEqual(series[-1]["points"][0]["value"]["int64Value"], str(int(completed.timestamp())))

    def test_scanner_error_creates_no_complete_snapshot(self):
        version = subprocess.CompletedProcess([], 0, "Version: test\n", "")
        failure = subprocess.CompletedProcess([], 1, "", "scanner database unavailable")
        with patch.object(cve.subprocess, "run", side_effect=[version, failure]):
            with self.assertRaises(RuntimeError):
                cve.run_scans(["sandbox:v1"], self.root)
        self.assertFalse((self.root / "snapshot.json").exists())

    def test_success_of_first_image_does_not_hide_second_scan_failure(self):
        def run(command, **kwargs):
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "Version: test\n", "")
            if command[-1] == "sandbox:first":
                Path(command[command.index("--output") + 1]).write_text(
                    '{"SchemaVersion": 2, "Results": []}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 1, "", "unavailable")
        with patch.object(cve.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError):
                cve.run_scans(["sandbox:first", "sandbox:second"], self.root)
        self.assertFalse((self.root / "snapshot.json").exists())

    def test_entire_scope_success_is_verifiable(self):
        def run(command, **kwargs):
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "Version: test\n", "")
            self.assertEqual(command[command.index("--exit-code") + 1], "0")
            self.assertIn("--ignore-unfixed=false", command)
            Path(command[command.index("--output") + 1]).write_text(
                '{"SchemaVersion": 2, "Results": []}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        with patch.object(cve.subprocess, "run", side_effect=run):
            snapshot_path = cve.run_scans(["sandbox:first", "sandbox:second"], self.root)
        self.assertEqual(len(cve.load_verified_snapshot(snapshot_path)["scope"]), 2)


if __name__ == "__main__":
    unittest.main()
