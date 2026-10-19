"""Behavioral safety and ASGI integration tests; no cluster or network endpoints."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "module-d/runtime"
spec = importlib.util.spec_from_file_location("module_d_runtime", PACKAGE / "__init__.py",
                                            submodule_search_locations=[str(PACKAGE)])
runtime = importlib.util.module_from_spec(spec)
sys.modules["module_d_runtime"] = runtime
spec.loader.exec_module(runtime)
chaos = sys.modules["module_d_runtime.chaos"]
SANDBOX = {"DEPLOYMENT_ENVIRONMENT": "sandbox", "POD_NAMESPACE": "observability-chaos-d"}


def profile(mode="latency", now=1000.0, identifier="test-one"):
    return {"enabled": True, "experiment_id": identifier, "mode": mode, "issued_at": now,
            "expires_at": now + 180, "delay_ms": 200 if mode == "latency" else 0,
            "error_rate": 0 if mode == "latency" else 0.10}


class ControlSafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "control.json"
        self.now, self.mono = [1000.0], [10.0]
        self.reader = chaos.ControlReader(self.path, "payments", dict(SANDBOX),
            wall_clock=lambda: self.now[0], monotonic_clock=lambda: self.mono[0])

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_absent_disabled_and_partial_are_off(self):
        self.assertIsNone(self.reader.read())
        for value in ({"enabled": False}, {"enabled": True}, None, []):
            self.write(value)
            self.assertIsNone(self.reader.read())

    def test_only_bounded_exact_profile_allowed(self):
        self.write(profile())
        self.assertEqual(self.reader.read()["delay_ms"], 200)
        for field, value in (("delay_ms", 201), ("delay_ms", -1), ("error_rate", 0.10),
                             ("mode", "unknown"), ("experiment_id", "too long " * 20)):
            document = profile(identifier="other")
            document[field] = value
            self.write(document)
            self.assertIsNone(self.reader.read())

    def test_future_expired_and_overlong_windows_are_off(self):
        for issued, expires in ((1001, 1100), (900, 1000), (1000, 1181), (1000, 1000), (1100, 1000)):
            document = profile()
            document.update(issued_at=issued, expires_at=expires)
            self.write(document)
            self.assertIsNone(self.reader.read())

    def test_nonfinite_and_boolean_numbers_are_off(self):
        for field in ("issued_at", "expires_at", "delay_ms", "error_rate"):
            for value in (float("nan"), float("inf"), float("-inf"), True, False):
                document = profile()
                document[field] = value
                self.write(document)
                self.assertIsNone(self.reader.read(), (field, value))
        document = profile()
        document["enabled"] = 1
        self.write(document)
        self.assertIsNone(self.reader.read())

    def test_oversized_malformed_duplicate_and_extra_config_are_off(self):
        for text in (" " * 4097, "{broken", '{"enabled":false,"enabled":true}', '\ufeff{}'):
            self.path.write_text(text, encoding="utf-8")
            self.assertIsNone(self.reader.read())
        self.write({**profile(), "unrecognized": True})
        self.assertIsNone(self.reader.read())

    def test_outside_exact_sandbox_is_off(self):
        self.write(profile())
        for env in ({}, {**SANDBOX, "DEPLOYMENT_ENVIRONMENT": "production"},
                    {**SANDBOX, "POD_NAMESPACE": "observability-lab"}):
            self.reader.environment = env
            self.assertIsNone(self.reader.read())

    def test_role_cannot_target_wrong_service(self):
        self.write(profile("error"))
        self.assertIsNone(self.reader.read())
        self.reader.role = "data"
        self.assertEqual(self.reader.read()["error_rate"], 0.10)
        self.reader.role = "observer"
        self.assertIsNone(self.reader.read())

    def test_same_id_cannot_extend_or_change_accepted_control(self):
        self.write(profile())
        self.assertIsNotNone(self.reader.read())
        self.now[0] += 10
        self.write(profile(now=1010))
        self.assertIsNone(self.reader.read())

    def test_backward_wall_clock_does_not_extend_monotonic_deadline(self):
        self.write(profile())
        self.assertIsNotNone(self.reader.read())
        self.mono[0] += 181
        self.assertIsNone(self.reader.read())

    def test_file_can_be_disabled_after_activation(self):
        self.write(profile())
        self.assertIsNotNone(self.reader.read())
        self.write({"enabled": False})
        self.assertIsNone(self.reader.read())


class RouteAndMetricsTests(unittest.TestCase):
    def test_exact_business_routes_include_data_health(self):
        policy = chaos.RoutePolicy('{"GET":["/data/health"],"POST":["/payments"]}')
        self.assertTrue(policy.matches("GET", "/data/health"))
        self.assertTrue(policy.matches("POST", "/payments"))
        self.assertFalse(policy.matches("GET", "/payments"))
        self.assertFalse(policy.matches("POST", "/payments/"))

    def test_reserved_and_unbounded_routes_rejected(self):
        for path in ("/health", "/healthz", "/docs", "/metrics", "/__module_d/metrics", "/items/{id}", "/x?q=1"):
            with self.assertRaises(ValueError):
                chaos.RoutePolicy({"GET": [path]})
        with self.assertRaises(ValueError):
            chaos.RoutePolicy({"DELETE": ["/data"]})

    def test_histogram_is_cumulative_and_has_exact_slo_bucket(self):
        metrics = chaos.Metrics()
        metrics.observe("module-d-data", "/data/health", "GET", 200, 0.1)
        metrics.observe("module-d-data", "/data/health", "GET", 503, 0.25)
        text = metrics.render().decode()
        self.assertIn('status_code="503"} 1', text)
        self.assertIn('le="0.2"} 1', text)
        self.assertIn('le="0.25"} 2', text)
        self.assertIn('le="+Inf"} 2', text)
        self.assertIn('module_d_http_request_duration_seconds_sum{service="module-d-data",route="/data/health",method="GET"} 0.350000000', text)
        self.assertNotIn("experiment_id", text)


class ASGIBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "control.json"
        self.provider = TracerProvider()
        self.exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)
        self.events, self.calls = [], []

    def wrapper(self, mode=None, app=None, random_value=lambda: 0.0, role=None):
        if mode:
            self.path.write_text(json.dumps(profile(mode, time.time())), encoding="utf-8")
        async def original(scope, receive, send):
            self.calls.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"original business response"})
        return chaos.ChaosMiddleware(app or original, "module-d-data" if mode == "error" else "module-d-payments",
            {"POST": ["/payments"], "GET": ["/data/health"]},
            control=chaos.ControlReader(self.path, role or ("data" if mode == "error" else "payments"), dict(SANDBOX)),
            tracer=self.provider.get_tracer("test-runtime"), random_value=random_value, event_sink=self.events.append)

    async def request(self, app, path="/payments", method="POST", headers=None):
        messages = []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "path": path, "method": method,
                 "headers": headers or [], "query_string": b"", "scheme": "http", "server": ("sandbox", 8001),
                 "http_version": "1.1", "root_path": "", "client": ("127.0.0.1", 40000)}
        async def receive():
            return {"type": "http.request", "body": b'{"secret":"never-log-this"}', "more_body": False}
        async def send(message):
            messages.append(message)
        await app(scope, receive, send)
        return messages

    async def test_disabled_passes_body_response_and_records_once(self):
        wrapper = self.wrapper()
        response = await self.request(wrapper)
        self.assertEqual(response[0]["status"], 200)
        self.assertEqual(response[1]["body"], b"original business response")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(wrapper.metrics.requests.values()), 1)
        self.assertFalse(self.events)

    async def test_latency_adds_wait_before_app_and_measures_it(self):
        wrapper = self.wrapper("latency")
        started = time.perf_counter()
        response = await self.request(wrapper)
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.180)
        self.assertEqual(response[0]["status"], 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.events), 1)
        self.assertGreaterEqual(next(iter(wrapper.metrics.durations.values()))["sum"], 0.180)
        spans = self.exporter.get_finished_spans()
        self.assertEqual(spans[0].kind.name, "INTERNAL")

    async def test_injected_503_does_not_invoke_application_or_read_body(self):
        wrapper = self.wrapper("error")
        response = await self.request(wrapper, "/data/health", "GET")
        self.assertEqual(response[0]["status"], 503)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.events), 1)
        self.assertNotIn("never-log-this", json.dumps(self.events))
        self.assertEqual(self.exporter.get_finished_spans()[0].kind.name, "SERVER")

    async def test_nonselected_request_preserves_original_response(self):
        wrapper = self.wrapper("error", random_value=lambda: 0.999)
        response = await self.request(wrapper, "/data/health", "GET")
        self.assertEqual(response[0]["status"], 200)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.events)

    async def test_rejected_response_trace_matches_real_exported_span_and_parent(self):
        wrapper = self.wrapper("error")
        incoming = "1234567890abcdef1234567890abcdef"
        response = await self.request(wrapper, "/data/health", "GET",
            [(b"traceparent", f"00-{incoming}-1234567890abcdef-01".encode())])
        span = self.exporter.get_finished_spans()[0]
        event = self.events[0]
        self.assertEqual(event["trace_id"], incoming)
        self.assertEqual(event["span_id"], f"{span.context.span_id:016x}")
        self.assertEqual(dict(response[0]["headers"])[b"x-trace-id"].decode(), incoming)
        self.assertEqual(span.parent.span_id, int("1234567890abcdef", 16))
        self.assertRegex(event["timestamp"], r"T\d{2}:\d{2}:\d{2}\.\d{9}Z$")
        self.assertEqual(chaos.utc_nanoseconds(event["observed_unix_ns"]), event["timestamp"])

    async def test_normal_request_forwards_parent_without_extra_server_span(self):
        wrapper = self.wrapper()
        await self.request(wrapper, headers=[(b"baggage", b"lab=module-d")])
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.kind.name, "INTERNAL")
        forwarded = dict(self.calls[0]["headers"])[b"traceparent"].decode()
        self.assertIn(f"{span.context.trace_id:032x}-{span.context.span_id:016x}", forwarded)
        self.assertEqual(dict(self.calls[0]["headers"])[b"baggage"], b"lab=module-d")

    async def test_instrumented_fastapi_keeps_one_server_span_in_same_trace(self):
        from fastapi import FastAPI
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        app = FastAPI()
        @app.post("/payments")
        async def payment():
            return {"status": "approved"}
        FastAPIInstrumentor.instrument_app(app, tracer_provider=self.provider)
        wrapper = self.wrapper("latency", app=app)
        response = await self.request(wrapper)
        spans = self.exporter.get_finished_spans()
        servers = [span for span in spans if span.kind.name == "SERVER"]
        guards = [span for span in spans if span.name == "module-d.guard"]
        self.assertEqual(response[0]["status"], 200)
        self.assertEqual(len(servers), 1)
        self.assertEqual(len(guards), 1)
        self.assertEqual(servers[0].parent.span_id, guards[0].context.span_id)
        self.assertEqual(servers[0].context.trace_id, guards[0].context.trace_id)
        self.assertEqual(self.events[0]["trace_id"], f"{servers[0].context.trace_id:032x}")
        self.assertEqual(dict(response[0]["headers"])[b"x-trace-id"].decode(), self.events[0]["trace_id"])

    async def test_health_unknown_route_and_wrong_method_are_not_counted(self):
        wrapper = self.wrapper("latency")
        await self.request(wrapper, "/health", "GET")
        await self.request(wrapper, "/not-business", "POST")
        await self.request(wrapper, "/payments", "GET")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(wrapper.metrics.requests, {})
        self.assertEqual(self.events, [])

    async def test_scrape_does_not_call_business_app_or_contaminate_metrics(self):
        wrapper = self.wrapper()
        response = await self.request(wrapper, chaos.METRICS_PATH, "GET")
        self.assertEqual(response[0]["status"], 200)
        self.assertIn(b"# TYPE module_d_http_requests_total counter", response[1]["body"])
        self.assertEqual(self.calls, [])
        self.assertEqual(wrapper.metrics.requests, {})

    async def test_application_exception_propagates_and_records_failure_once(self):
        async def failing(scope, receive, send):
            raise RuntimeError("business app failure")
        wrapper = self.wrapper(app=failing)
        with self.assertRaisesRegex(RuntimeError, "business app failure"):
            await self.request(wrapper)
        self.assertEqual(sum(wrapper.metrics.requests.values()), 1)
        self.assertEqual(next(iter(wrapper.metrics.requests))[3], "500")

    async def test_lifespan_delegated_without_http_metrics(self):
        messages = []
        async def lifespan(scope, receive, send):
            self.assertEqual(scope["type"], "lifespan")
            event = await receive()
            self.assertEqual(event["type"], "lifespan.startup")
            await send({"type": "lifespan.startup.complete"})
        wrapper = self.wrapper("error", app=lifespan)
        async def receive():
            return {"type": "lifespan.startup"}
        async def send(message):
            messages.append(message)
        await wrapper({"type": "lifespan"}, receive, send)
        self.assertEqual(messages, [{"type": "lifespan.startup.complete"}])
        self.assertEqual(wrapper.metrics.requests, {})

    async def test_lifespan_error_is_not_replaced_by_fault(self):
        async def lifecycle_error(scope, receive, send):
            raise RuntimeError("startup failure")
        wrapper = self.wrapper("error", app=lifecycle_error)
        with self.assertRaisesRegex(RuntimeError, "startup failure"):
            await wrapper({"type": "lifespan"}, None, None)
        self.assertFalse(self.events)


class EntrypointTests(unittest.TestCase):
    def test_imports_original_app_with_explicit_reference_and_no_factory_execution(self):
        from opentelemetry.sdk.trace import TracerProvider
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        original_module = types.ModuleType("module_d_fixture_application")
        async def app(scope, receive, send):
            return None
        original_module.app = app
        env = {**SANDBOX, "MODULE_D_APP": "module_d_fixture_application:app",
               "MODULE_D_SERVICE_NAME": "module-d-data", "MODULE_D_ROLE": "data",
               "MODULE_D_ROUTES": '{"GET":["/data/health"]}', "OTEL_SDK_DISABLED": "false"}
        spec = importlib.util.spec_from_file_location("module_d_runtime.entrypoint", PACKAGE / "entrypoint.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"module_d_fixture_application": original_module}), patch.dict(os.environ, env), patch(
            "opentelemetry.trace.get_tracer_provider", return_value=provider
        ):
            spec.loader.exec_module(module)
            self.assertIs(module.app.app, app)
            self.assertEqual(module.app.control.role, "data")
            with self.assertRaises(ValueError):
                module.load_real_app("module_d_fixture_application:app()")
            with patch.dict(os.environ, {"POD_NAMESPACE": "shared"}), self.assertRaises(RuntimeError):
                module.build_app()


if __name__ == "__main__":
    unittest.main()
