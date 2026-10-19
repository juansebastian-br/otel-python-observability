"""Real /orders ASGI route checks with local in-memory OTel exporters.

Database and payment calls are substituted; no cloud or database is contacted.
"""
from contextlib import redirect_stdout
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("auth_probe", ROOT / "module-c/security/auth_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeBoundaryTests(unittest.TestCase):
    def test_probe_receipt_has_utc_window_and_per_request_timestamps(self):
        from datetime import datetime
        import urllib.error
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("http://127.0.0.1/orders", 401, "Unauthorized", {}, None)
        output = io.StringIO()
        with patch.object(sys, "argv", ["auth_probe.py", "--count", "1"]), patch.object(
            probe.urllib.request, "build_opener", return_value=opener
        ), redirect_stdout(output):
            probe.main()
        request, receipt = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(request["status"], 401)
        self.assertEqual(receipt["result"], "PASS")
        self.assertEqual(receipt["trace_ids"], [request["trace_id"]])
        values = [receipt["started_at"], request["sent_at"], request["completed_at"], receipt["completed_at"]]
        instants = [datetime.fromisoformat(value) for value in values]
        self.assertTrue(all(value.utcoffset().total_seconds() == 0 for value in instants))
        self.assertEqual(instants, sorted(instants))
        self.assertGreaterEqual(receipt["duration_seconds"], 0)

    def test_loopback_allowed(self):
        self.assertEqual(probe.validate_target("http://127.0.0.1:8000"), "http://127.0.0.1:8000/orders")
        self.assertEqual(probe.validate_target("http://[::1]:8000/orders"), "http://[::1]:8000/orders")

    def test_remote_requires_exact_explicit_authorized_host(self):
        for allowed in (None, "other.example"):
            with self.assertRaises(ValueError):
                probe.validate_target("https://sandbox.example", allowed)
        self.assertEqual(probe.validate_target("https://sandbox.example", "sandbox.example"), "https://sandbox.example/orders")

    def test_other_routes_credentials_and_queries_rejected(self):
        for url in ("http://localhost/admin", "http://user:secret@localhost", "http://localhost?token=abc"):
            with self.assertRaises(ValueError):
                probe.validate_target(url)


class AuthenticationRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        cls.key = "test-only-sandbox-key-at-least-24-bytes"
        cls.environment = patch.dict(os.environ, {
            "SECURITY_AUTH_ENABLED": "true", "SECURITY_API_KEY": cls.key,
            "DEPLOYMENT_ENVIRONMENT": "test", "DATABASE_URL": "sqlite://",
            "OTEL_SDK_DISABLED": "false", "OTEL_SERVICE_NAME": "orders-service",
        })
        cls.environment.start()
        otel = importlib.import_module("services.orders.app.otel")
        cls.spans = InMemorySpanExporter()
        cls.logs = InMemoryLogRecordExporter()
        otel.OTLPSpanExporter = lambda **kwargs: cls.spans
        otel.OTLPLogExporter = lambda **kwargs: cls.logs
        otel.OTLPMetricExporter = lambda **kwargs: object()
        otel.PeriodicExportingMetricReader = lambda *args, **kwargs: InMemoryMetricReader()
        cls.main = importlib.import_module("services.orders.app.main")
        cls.security = importlib.import_module("services.orders.app.security")
        cls.client = TestClient(cls.main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.environment.stop()

    def request(self, headers=None):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            response = self.client.post("/orders", json={"customer_id": "test", "amount": 1}, headers=headers or {})
        events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.startswith("{")]
        return response, events

    def test_health_is_exempt(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_missing_key_rejected_before_database(self):
        with patch.object(self.main, "engine") as engine:
            response, events = self.request()
        self.assertEqual(response.status_code, 401)
        engine.begin.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "missing_credential")

    def test_invalid_key_emits_one_event_with_actual_trace_and_no_secret(self):
        from opentelemetry import trace
        trace_id = "1234567890abcdef1234567890abcdef"
        response, events = self.request({
            "X-API-Key": "rejected-secret-must-never-appear",
            "traceparent": f"00-{trace_id}-1234567890abcdef-01",
        })
        trace.get_tracer_provider().force_flush()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "authentication_failed")
        self.assertEqual(event["service_name"], "orders-service")
        self.assertEqual(event["reason"], "invalid_credential")
        self.assertEqual(event["trace_id"], trace_id)
        self.assertIn(event["span_id"], {f"{span.context.span_id:016x}" for span in self.spans.get_finished_spans()})
        self.assertNotIn("rejected-secret", json.dumps(event))
        self.assertNotIn(self.key, json.dumps(event))

    def test_correct_key_executes_real_orders_route(self):
        engine = MagicMock()
        client = MagicMock()
        with patch.object(self.main, "engine", engine), patch.object(self.main, "httpx", types.SimpleNamespace(Client=client)):
            response, events = self.request({"X-API-Key": self.key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PAID")
        self.assertEqual(engine.begin.call_count, 2)
        self.assertEqual(events, [])

    def test_non_ascii_key_rejects_cleanly(self):
        # Header parsing ordinarily permits latin-1. The guard compares UTF-8
        # bytes so non-ASCII input cannot crash compare_digest.
        from starlette.requests import Request
        import asyncio
        request = Request({"type": "http", "headers": [(b"x-api-key", b"\xff") ]})
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(Exception) as raised:
            asyncio.run(self.main.require_api_key(request))
        self.assertEqual(raised.exception.status_code, 401)

    def test_missing_secret_and_production_mode_fail_startup(self):
        for values in ({"SECURITY_API_KEY": ""}, {"DEPLOYMENT_ENVIRONMENT": "production"}):
            with patch.dict(os.environ, values), self.assertRaises(RuntimeError):
                self.security.build_api_key_guard()

    def test_disabled_is_compatible_without_key(self):
        from starlette.requests import Request
        import asyncio
        with patch.dict(os.environ, {"SECURITY_AUTH_ENABLED": "false", "SECURITY_API_KEY": ""}):
            guard = self.security.build_api_key_guard()
        request = Request({"type": "http", "headers": []})
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertIsNone(asyncio.run(guard(request)))
        self.assertEqual(stdout.getvalue(), "")


class SandboxWorkloadTests(unittest.TestCase):
    def test_independent_workload_correlates_rejections_and_accepts_valid_key(self):
        from fastapi.testclient import TestClient
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        exporter = InMemorySpanExporter()
        key = "standalone-test-key-at-least-24-bytes"
        environment = {"SECURITY_AUTH_ENABLED": "true", "SECURITY_API_KEY": key,
                       "DEPLOYMENT_ENVIRONMENT": "test", "OTEL_SERVICE_NAME": "module-c-auth-probe"}
        probe_spec = importlib.util.spec_from_file_location("probe_app", ROOT / "module-c/security/probe_app.py")
        module = importlib.util.module_from_spec(probe_spec)
        with patch.dict(os.environ, environment), patch(
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter", return_value=exporter
        ):
            probe_spec.loader.exec_module(module)
        with TestClient(module.app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            output = io.StringIO()
            trace_id = "abcdef1234567890abcdef1234567890"
            with redirect_stdout(output):
                rejected = client.post("/orders", headers={"traceparent": f"00-{trace_id}-1234567890abcdef-01"})
            self.assertEqual(rejected.status_code, 401)
            event = json.loads(output.getvalue())
            self.assertEqual(event["trace_id"], trace_id)
            self.assertEqual(event["service_name"], "module-c-auth-probe")
            accepted = client.post("/orders", headers={"x-api-key": key})
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["status"], "authenticated")
            module.provider.force_flush()
            self.assertIn(event["span_id"], {f"{span.context.span_id:016x}" for span in exporter.get_finished_spans()})


if __name__ == "__main__":
    unittest.main()
