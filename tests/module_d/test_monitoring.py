import argparse
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[2] / "module-d" / "monitoring"


def load(name):
    spec = importlib.util.spec_from_file_location("module_d_monitoring_" + name, BASE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render, receiver, capture = (load(name) for name in ("render", "receiver", "capture"))


def config():
    return {"namespace": "observability-chaos-d", "receiver_image": "test/approved@sha256:" + "a" * 64,
        "latency_threshold_ms": 200, "minimum_samples": 20,
        "payments": {"service": "module-d-payments", "route": "/payments", "method": "POST", "target": "module-d-payments:8001"},
        "data": {"service": "module-d-data", "route": "/unit-test-route", "method": "POST", "target": "module-d-data:8002"}}


class MonitoringTests(unittest.TestCase):
    def test_separate_rules_strict_services_routes_and_sample_floor(self):
        result = render.build(config())
        latency, error, missing = result["rules"]["groups"][0]["rules"]
        self.assertIn("histogram_quantile(0.99", latency["expr"])
        self.assertIn("> 0.2", latency["expr"])
        self.assertNotIn("status_code", latency["expr"])
        self.assertIn('service="module-d-data",route="/unit-test-route",method="POST",status_code=~"5.."', error["expr"])
        self.assertIn("> 0.005", error["expr"])
        self.assertNotIn("histogram_quantile", error["expr"])
        self.assertIn("or on (service, route, method)", error["expr"])
        self.assertIn("* 0", error["expr"])
        for rule in (latency, error):
            self.assertIn(">= 20", rule["expr"])
            self.assertIn("[60s]", rule["expr"])
            self.assertEqual(rule["for"], "30s")
        self.assertEqual(missing["alert"], "ModuleDScrapeUnavailable")
        self.assertEqual(result["prometheus"]["global"]["scrape_interval"], "5s")
        self.assertEqual(result["prometheus"]["scrape_configs"][0]["metrics_path"], "/__module_d/metrics")

    def test_refuses_shared_namespace_external_targets_and_unapproved_metric_identity(self):
        for alteration in (lambda c: c.update(namespace="observability-lab"),
            lambda c: c["data"].update(target="real.shared.svc.cluster.local:8000"),
            lambda c: c["data"].update(service="data-service"),
            lambda c: c.update(receiver_image="python:latest"),
            lambda c: c.update(minimum_samples=1)):
            c = config(); alteration(c)
            with self.assertRaises(ValueError):
                render.build(c)

    def test_only_internal_unprivileged_resources_and_no_service_account_tokens(self):
        items = render.build(config())["manifest"]["items"]
        for item in items:
            self.assertEqual(item["metadata"]["namespace"], "observability-chaos-d")
            self.assertEqual(item["metadata"]["labels"]["app.kubernetes.io/part-of"], "module-d")
            if item["kind"] == "Deployment":
                pod = item["spec"]["template"]["spec"]
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertTrue(pod["containers"][0]["securityContext"]["runAsNonRoot"])
            if item["kind"] == "Service":
                self.assertEqual(item["spec"]["type"], "ClusterIP")
        self.assertFalse(any(i["kind"] in ("Role", "RoleBinding", "ClusterRole", "Secret") for i in items))

    def test_receiver_keeps_actual_receipt_separate_from_pending_start(self):
        payload = {"status": "firing", "alerts": [{"labels": {"namespace": "observability-chaos-d", "alertname": "ModuleDLatency"},
            "status": "firing", "startsAt": "2026-01-01T00:00:00Z", "untrusted_extra": "excluded"}]}
        event = receiver.normalize(payload, "2026-01-01T00:00:35Z")
        self.assertEqual(event["received_at"], "2026-01-01T00:00:35Z")
        self.assertEqual(event["alerts"][0]["startsAt"], "2026-01-01T00:00:00Z")
        self.assertNotIn("untrusted_extra", event["alerts"][0])
        for invalid_alert in ({"status": "unknown"}, {"status": "firing", "labels": []}, "invalid"):
            altered = copy.deepcopy(payload); altered["alerts"] = [invalid_alert]
            with self.assertRaises(ValueError):
                receiver.normalize(altered, "2026-01-01T00:00:35Z")
        for changed in ({"namespace": "shared", "alertname": "ModuleDLatency"}, {"namespace": "observability-chaos-d", "alertname": "Unrelated"}):
            altered = copy.deepcopy(payload); altered["alerts"][0]["labels"] = changed
            with self.assertRaises(ValueError):
                receiver.normalize(altered, "2026-01-01T00:00:35Z")

    def test_capture_requires_local_forward_without_credentials(self):
        self.assertEqual(capture.local_url("http://127.0.0.1:19090/"), "http://127.0.0.1:19090")
        for url in ("https://cloud.example/", "http://user:pass@localhost:8080", "http://localhost:8080/path", "http://localhost:8080?token=test"):
            with self.assertRaises(argparse.ArgumentTypeError):
                capture.local_url(url)

    def test_stop_file_preserves_final_evidence_without_waiting_for_duration(self):
        temporary_root = BASE.parents[1] / ".state" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            folder = Path(temporary)
            stop = folder / "stop"
            stop.write_text("complete", encoding="utf-8")
            args = argparse.Namespace(out=folder / "capture", seconds=120, poll_seconds=2,
                stop_file=stop, prometheus="http://127.0.0.1:19090", alertmanager="http://127.0.0.1:19093",
                receiver="http://127.0.0.1:18080")
            with patch.object(capture, "fetch", return_value={"events": []}), patch("builtins.print"):
                capture.capture(args)
            observation = json.loads((args.out / "observation.json").read_text(encoding="utf-8"))
            self.assertEqual(observation["stop_reason"], "stop_file_observed")
            self.assertIsNone(observation["mttd_seconds"])
            self.assertTrue((args.out / "receiver-final.json").exists())
            self.assertTrue((args.out / "rules-final.json").exists())


if __name__ == "__main__":
    unittest.main()
