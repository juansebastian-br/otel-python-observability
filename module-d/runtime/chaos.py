"""Application-level faults without privileged networking or control endpoints."""
import asyncio
from collections import OrderedDict
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time

try:
    from opentelemetry import context as otel_context, propagate, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError:  # Importable for offline config/metrics tests.
    otel_context = propagate = trace = None

SANDBOX_NAMESPACE = "observability-chaos-d"
METRICS_PATH = "/__module_d/metrics"
MAX_CONTROL_BYTES = 4096
MAX_TTL_SECONDS = 180
EXEMPT_PATHS = ("/health", "/healthz", "/ready", "/readyz", "/live", "/livez",
                "/docs", "/redoc", "/openapi.json", "/metrics", "/__module_d")
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 1.0, 2.0, 5.0)


def utc_nanoseconds(value=None):
    value = time.time_ns() if value is None else value
    seconds, fraction = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _object_without_duplicates(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate control key")
        result[key] = value
    return result


def strict_json(text):
    def invalid_constant(value):
        raise ValueError("Nonfinite JSON number")
    return json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=invalid_constant)


class ControlReader:
    """Invalid, absent, expired, or inappropriate configuration means no fault.

    A monotonic deadline also prevents a backward wall-clock adjustment from
    extending a control that this process has already observed.
    """
    fields = {"enabled", "experiment_id", "mode", "issued_at", "expires_at", "delay_ms", "error_rate"}

    def __init__(self, path="/var/run/chaos/control.json", role="observer", environment=None,
                 wall_clock=time.time, monotonic_clock=time.monotonic):
        self.path = Path(path)
        self.role = role
        self.environment = os.environ if environment is None else environment
        self.wall_clock, self.monotonic_clock = wall_clock, monotonic_clock
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    def read(self):
        if (self.environment.get("DEPLOYMENT_ENVIRONMENT") != "sandbox"
                or self.environment.get("POD_NAMESPACE") != SANDBOX_NAMESPACE
                or self.role not in {"payments", "data"}):
            return None
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_CONTROL_BYTES + 1)
            if len(raw) > MAX_CONTROL_BYTES:
                return None
            control = strict_json(raw.decode("utf-8"))
            if not isinstance(control, dict) or type(control.get("enabled")) is not bool:
                return None
            if not control["enabled"]:
                return None
            if set(control) != self.fields:
                return None
            identifier = control["experiment_id"]
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", identifier):
                return None
            issued, expires = control["issued_at"], control["expires_at"]
            now, mono = self.wall_clock(), self.monotonic_clock()
            if not all(_finite_number(value) for value in (issued, expires, now, mono)):
                return None
            if not (0 < expires - issued <= MAX_TTL_SECONDS and issued <= now < expires):
                return None
            delay, error_rate = control["delay_ms"], control["error_rate"]
            if not _finite_number(delay) or not _finite_number(error_rate):
                return None
            latency = self.role == "payments" and control["mode"] == "latency" and delay == 200 and error_rate == 0
            error = self.role == "data" and control["mode"] == "error" and delay == 0 and error_rate == 0.10
            if not (latency or error):
                return None
            signature = json.dumps(control, sort_keys=True, separators=(",", ":"))
            with self._lock:
                previous = self._seen.get(identifier)
                if previous:
                    previous_signature, deadline = previous
                    if signature != previous_signature or mono >= deadline:
                        return None
                else:
                    # Bound memory and refuse additional IDs rather than evicting
                    # a deadline that could subsequently be replayed after a clock jump.
                    if len(self._seen) >= 256:
                        return None
                    self._seen[identifier] = (signature, mono + expires - now)
            return control
        except (OSError, ValueError, UnicodeError, TypeError, OverflowError):
            return None


class RoutePolicy:
    def __init__(self, routes):
        if isinstance(routes, str):
            routes = strict_json(routes)
        if not isinstance(routes, dict) or not routes:
            raise ValueError("MODULE_D_ROUTES must be a nonempty JSON method-to-paths object")
        self.allowed = set()
        for method, paths in routes.items():
            if method not in {"GET", "POST"} or not isinstance(paths, list) or not paths:
                raise ValueError("Only explicit GET/POST route lists are supported")
            for path in paths:
                if (not isinstance(path, str) or not path.startswith("/") or len(path) > 160
                        or any(character in path for character in ("?", "#", "{", "}", "\n", "\r"))):
                    raise ValueError("Routes must be exact business paths without query or template variables")
                if self.exempt(path):
                    raise ValueError("Health, docs and metrics paths cannot be business targets")
                self.allowed.add((method, path))
        if len(self.allowed) > 16:
            raise ValueError("At most 16 explicit business method/path pairs are supported")

    @staticmethod
    def exempt(path):
        return any(path == prefix or path.startswith(prefix + "/") for prefix in EXEMPT_PATHS)

    def matches(self, method, path):
        return not self.exempt(path) and (method, path) in self.allowed


def _labels(values):
    return "{" + ",".join(f'{key}={json.dumps(str(value))}' for key, value in values.items()) + "}"


class Metrics:
    """Small, process-local Prometheus registry; run one Uvicorn worker per pod."""
    def __init__(self):
        self.requests, self.durations, self.injected = {}, {}, {}
        self._lock = threading.Lock()

    def observe(self, service, route, method, status, elapsed):
        key, duration_key = (service, route, method, str(status)), (service, route, method)
        elapsed = max(0.0, elapsed)
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1
            record = self.durations.setdefault(duration_key, {"count": 0, "sum": 0.0, "buckets": [0] * len(BUCKETS)})
            record["count"] += 1
            record["sum"] += elapsed
            for index, bound in enumerate(BUCKETS):
                if elapsed <= bound:
                    record["buckets"][index] += 1

    def applied(self, service, route, mode):
        with self._lock:
            key = (service, route, mode)
            self.injected[key] = self.injected.get(key, 0) + 1

    def render(self):
        lines = ["# HELP module_d_http_requests_total Completed or interrupted allowlisted requests.",
                 "# TYPE module_d_http_requests_total counter",
                 "# HELP module_d_http_request_duration_seconds Full wrapper duration including injected delay.",
                 "# TYPE module_d_http_request_duration_seconds histogram",
                 "# HELP module_d_chaos_applied_total Requests actually affected by a bounded experiment.",
                 "# TYPE module_d_chaos_applied_total counter"]
        with self._lock:
            for key, count in sorted(self.requests.items()):
                labels = dict(zip(("service", "route", "method", "status_code"), key))
                lines.append(f"module_d_http_requests_total{_labels(labels)} {count}")
            for key, record in sorted(self.durations.items()):
                labels = dict(zip(("service", "route", "method"), key))
                for bound, count in zip(BUCKETS, record["buckets"]):
                    lines.append(f"module_d_http_request_duration_seconds_bucket{_labels({**labels, 'le': format(bound, 'g')})} {count}")
                lines.append(f"module_d_http_request_duration_seconds_bucket{_labels({**labels, 'le': '+Inf'})} {record['count']}")
                lines.append(f"module_d_http_request_duration_seconds_count{_labels(labels)} {record['count']}")
                lines.append(f"module_d_http_request_duration_seconds_sum{_labels(labels)} {record['sum']:.9f}")
            for key, count in sorted(self.injected.items()):
                labels = dict(zip(("service", "route", "mode"), key))
                lines.append(f"module_d_chaos_applied_total{_labels(labels)} {count}")
        return ("\n".join(lines) + "\n").encode("utf-8")


class ChaosMiddleware:
    def __init__(self, app, service, routes, control=None, metrics=None, tracer=None,
                 random_value=random.random, sleeper=asyncio.sleep, clock=time.perf_counter,
                 event_sink=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", service):
            raise ValueError("MODULE_D_SERVICE_NAME must be a stable nonempty identifier")
        self.app, self.service = app, service
        self.routes = routes if isinstance(routes, RoutePolicy) else RoutePolicy(routes)
        self.control = control or ControlReader()
        self.metrics = metrics or Metrics()
        self.tracer = tracer or (trace.get_tracer("module-d.runtime") if trace else None)
        self.random_value, self.sleeper, self.clock = random_value, sleeper, clock
        self.event_sink = event_sink or (lambda event: print(json.dumps(event, separators=(",", ":")), flush=True))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, route = scope.get("method", ""), scope.get("path", "")
        if route == METRICS_PATH and method in {"GET", "HEAD"}:
            body = self.metrics.render() if method == "GET" else b""
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; version=0.0.4; charset=utf-8"),
                                    (b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": body})
        if not self.routes.matches(method, route):
            return await self.app(scope, receive, send)
        started = self.clock()
        status = 500
        control = self.control.read()
        injected_error = bool(control and control["mode"] == "error" and self.random_value() < 0.10)
        carrier = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        context = propagate.extract(carrier) if propagate else None
        span_manager = (self.tracer.start_as_current_span(
            "module-d.injected-response" if injected_error else "module-d.guard", context=context,
            kind=SpanKind.SERVER if injected_error else SpanKind.INTERNAL,
            attributes={"http.request.method": method, "http.route": route,
                        "module_d.service": self.service, "module_d.sandbox": True},
        ) if self.tracer else nullcontext(None))
        with span_manager as span:
            ctx = span.get_span_context() if span else None
            trace_id = f"{ctx.trace_id:032x}" if ctx and ctx.is_valid else None
            async def observed_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message = dict(message)
                    headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"x-trace-id"]
                    if trace_id:
                        headers.append((b"x-trace-id", trace_id.encode("ascii")))
                    message["headers"] = headers
                await send(message)
            try:
                applies = control and (control["mode"] == "latency" or injected_error)
                if applies:
                    self.metrics.applied(self.service, route, control["mode"])
                    nanoseconds = time.time_ns()
                    event = {"timestamp": utc_nanoseconds(nanoseconds), "observed_unix_ns": nanoseconds,
                             "event": "chaos.applied", "severity": "WARNING", "service_name": self.service,
                             "namespace": SANDBOX_NAMESPACE, "route": route, "method": method,
                             "experiment_id": control["experiment_id"], "mode": control["mode"],
                             "delay_ms": control["delay_ms"], "error_rate": control["error_rate"],
                             "trace_id": trace_id, "span_id": f"{ctx.span_id:016x}" if ctx and ctx.is_valid else None}
                    if span:
                        span.set_attribute("chaos.experiment_id", control["experiment_id"])
                        span.set_attribute("chaos.mode", control["mode"])
                        span.add_event("chaos.applied", {"experiment_id": control["experiment_id"]})
                    self.event_sink(event)
                    if injected_error:
                        body = b'{"detail":"Controlled sandbox fault"}'
                        await observed_send({"type": "http.response.start", "status": 503,
                                             "headers": [(b"content-type", b"application/json"),
                                                         (b"content-length", str(len(body)).encode("ascii"))]})
                        return await observed_send({"type": "http.response.body", "body": body})
                    await self.sleeper(0.200)
                # Preserve the wrapper span as parent for an already instrumented
                # application. Normal requests still have only the app's SERVER span.
                if propagate and ctx and ctx.is_valid:
                    child_carrier = {}
                    propagate.inject(child_carrier, context=trace.set_span_in_context(span, context))
                    forwarded = dict(scope)
                    forwarded["headers"] = [(key, value) for key, value in scope.get("headers", [])
                                            if key.lower() not in {b"traceparent", b"tracestate", b"baggage"}]
                    forwarded["headers"] += [(key.encode("ascii"), value.encode("latin-1")) for key, value in child_carrier.items()]
                else:
                    forwarded = scope
                # The OTel ASGI instrumentor chooses SERVER only when there is
                # no active local span. Pass the guard parent in W3C headers,
                # then temporarily clear the local span so the real application
                # creates its one SERVER span rather than downgrading it.
                token = otel_context.attach(trace.set_span_in_context(trace.INVALID_SPAN)) if trace else None
                try:
                    await self.app(forwarded, receive, observed_send)
                finally:
                    if token is not None:
                        otel_context.detach(token)
            except asyncio.CancelledError:
                status = 499
                raise
            finally:
                elapsed = max(0.0, self.clock() - started)
                self.metrics.observe(self.service, route, method, status, elapsed)
                if span:
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR))
