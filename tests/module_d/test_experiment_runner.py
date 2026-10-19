import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "module-d"))
from experiment import runner


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"{}"
        if self.path == "/slow":
            time.sleep(0.32)
        status = {"/error": 503, "/unauthorized": 401, "/redirect": 302}.get(self.path, 200)
        if self.path == "/large":
            body = b"x" * 4096
        try:
            self.send_response(status)
            if self.path == "/redirect":
                self.send_header("Location", "https://example.com/forbidden")
            if self.path != "/no-trace":
                self.send_header("x-trace-id", self.headers["traceparent"].split("-")[1])
            if self.path == "/drip":
                self.send_header("Content-Length", "10")
                self.end_headers()
                for _ in range(10):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def config(self, path="/ok", **kwargs):
        return runner.Config(runner.validate_target(self.origin, path, "127.0.0.1"),
                             "unit-experiment", "GET", b"{}", baseline_seconds=0.2,
                             chaos_seconds=0, recovery_seconds=0, **kwargs)

    def execute(self, config, **kwargs):
        stream = io.StringIO()
        result = runner.run(config, stream, **kwargs)
        return result, [json.loads(line) for line in stream.getvalue().splitlines()], stream.getvalue()

    def test_only_exact_loopback_and_sandbox_service_names_allowed(self):
        runner.validate_target("http://module-d-data.observability-chaos-d.svc:8002", "/data/health", "module-d-data.observability-chaos-d.svc")
        for url, path, allow in (
            ("http://example.com:80", "/", "example.com"),
            ("http://orders.observability-lab.svc:8000", "/orders", "orders.observability-lab.svc"),
            ("http://127.0.0.1:8000?secret=x", "/", "127.0.0.1"),
            ("http://token@127.0.0.1:8000", "/", "127.0.0.1"),
            ("http://127.0.0.1", "/", "127.0.0.1"),
            ("http://127.0.0.1:8000", "//example.com", "127.0.0.1"),
            ("http://127.0.0.1:8000", "/?token=x", "127.0.0.1"),
            ("http://127.0.0.1:8000", "/", "localhost"),
        ):
            with self.subTest(url=url, path=path), self.assertRaises(ValueError):
                runner.validate_target(url, path, allow)

    def test_trace_header_observation_is_explicit_and_secrets_are_not_serialized(self):
        config = self.config()
        config.headers = {"Authorization": "Bearer unit-private-value"}
        config.body = b'{"secret":"unit-body-secret"}'
        _, records, text = self.execute(config)
        request = next(r for r in records if r["record_type"] == "request")
        self.assertTrue(request["success"])
        self.assertEqual(request["sent_trace_id"], request["response_trace_id"])
        self.assertEqual(request["trace_correlation"], "response_header_matches")
        self.assertNotIn("unit-private-value", text)
        self.assertNotIn("unit-body-secret", text)

    def test_missing_trace_header_does_not_claim_correlation(self):
        _, records, _ = self.execute(self.config("/no-trace"))
        row = next(r for r in records if r["record_type"] == "request")
        self.assertIsNone(row["response_trace_id"])
        self.assertIsNone(row["trace_header_matches"])
        self.assertEqual(row["trace_correlation"], "not_observed")

    def test_http_failures_and_redirects_are_never_successful(self):
        for path, outcome in (("/error", "http_server_error"), ("/unauthorized", "http_client_error"), ("/redirect", "http_redirect")):
            with self.subTest(path=path):
                _, records, _ = self.execute(self.config(path))
                row = next(r for r in records if r["record_type"] == "request")
                self.assertEqual(row["outcome"], outcome)
                self.assertFalse(row["success"])

    def test_timeout_and_slow_drip_have_bounded_whole_request_duration(self):
        for path in ("/slow", "/drip"):
            with self.subTest(path=path):
                started = time.monotonic()
                _, records, _ = self.execute(self.config(path, timeout_seconds=0.08))
                row = next(r for r in records if r["record_type"] == "request")
                self.assertEqual(row["outcome"], "timeout")
                self.assertFalse(row["success"])
                self.assertTrue(row["latency_censored"])
                self.assertLess(time.monotonic() - started, 0.65)

    def test_saturation_records_unissued_arrival_instead_of_queueing(self):
        config = self.config("/slow", max_workers=1, timeout_seconds=1)
        config.baseline_seconds = 0.6
        result, records, _ = self.execute(config)
        self.assertEqual(result["planned_arrivals"], 3)
        self.assertGreaterEqual(result["arrival_missed"], 1)
        self.assertEqual(result["attempted"] + result["arrival_missed"], 3)
        self.assertTrue(all(not r["attempted"] for r in records if r["record_type"] == "arrival_missed"))

    def test_explicit_transition_does_not_become_artificial_schedule_loss(self):
        config = self.config()
        config.chaos_seconds = config.recovery_seconds = 0.2
        phases = []
        def hook(phase):
            phases.append(phase)
            time.sleep(0.04)
            return {"action": "unit-test-only"}
        result, records, _ = self.execute(config, phase_hook=hook)
        self.assertEqual(phases, ["baseline", "chaos", "recovery"])
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["arrival_missed"], 0)
        self.assertGreaterEqual(result["transition_seconds"], 0.12)
        self.assertEqual(sum(r["record_type"] == "phase_transition" for r in records), 3)

    def test_rejected_baseline_stops_before_chaos_requests(self):
        config = self.config()
        config.chaos_seconds = 0.2
        def hook(phase):
            if phase == "chaos":
                raise runner.PhaseTransitionError("baseline rejected")
        result, records, _ = self.execute(config, phase_hook=hook)
        self.assertFalse(result["completed"])
        self.assertTrue(result["phase_transition_failed"])
        self.assertEqual(result["unissued_after_stop"], 1)
        self.assertFalse(any(r.get("phase") == "chaos" and r["record_type"] == "request" for r in records))

    def test_response_limit_is_not_success(self):
        config = self.config("/large", response_byte_limit=128)
        _, records, _ = self.execute(config)
        row = next(r for r in records if r["record_type"] == "request")
        self.assertEqual(row["outcome"], "response_limit_exceeded")
        self.assertFalse(row["success"])

    def test_header_secrets_require_environment_and_reject_reserved_names(self):
        with patch.dict(os.environ, {"UNIT_TOKEN": "private-value"}):
            self.assertEqual(runner.headers_from_environment(["X-API-Key=UNIT_TOKEN"]), {"X-API-Key": "private-value"})
            for specs in (["Host=UNIT_TOKEN"], ["traceparent=UNIT_TOKEN"], ["Authorization=MISSING_UNIT_TOKEN"],
                          ["X-API-Key=UNIT_TOKEN", "x-api-key=UNIT_TOKEN"]):
                with self.assertRaises(ValueError):
                    runner.headers_from_environment(specs)

    def test_bounds_reject_unlimited_load_or_nonfinite_values(self):
        for field, value in (("rate", 6), ("rate", float("nan")), ("max_workers", 21),
                             ("chaos_seconds", 181), ("timeout_seconds", 0), ("baseline_seconds", 901)):
            config = self.config()
            setattr(config, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                config.validate()


if __name__ == "__main__":
    unittest.main()
