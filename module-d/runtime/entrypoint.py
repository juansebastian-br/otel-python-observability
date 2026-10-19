"""Uvicorn entrypoint overlay: module_d_runtime.entrypoint:app.

Copy this directory to a module_d_runtime package in the existing image. Its
original app is imported unchanged; no database or app endpoint is replaced.
"""
import importlib
import os
import re

from .chaos import ChaosMiddleware, ControlReader, RoutePolicy, SANDBOX_NAMESPACE


def load_real_app(reference):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", reference):
        raise ValueError("MODULE_D_APP must be an explicit module:attribute ASGI app reference")
    module_name, attribute = reference.split(":")
    app = getattr(importlib.import_module(module_name), attribute)
    if not callable(app):
        raise ValueError("MODULE_D_APP is not callable")
    return app


def ensure_tracing(service):
    from opentelemetry import trace
    provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        return None  # Reuse the application's configured SDK and lifecycle.
    # The app may be instrumented through an explicit local provider. A separate
    # wrapper SDK is necessary in that case; valid propagated context links both.
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("A configured OTel SDK or OTEL_EXPORTER_OTLP_ENDPOINT is required")
    provider = TracerProvider(resource=Resource.create({"service.name": service,
        "deployment.environment.name": "sandbox", "k8s.namespace.name": SANDBOX_NAMESPACE}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint,
        insecure=os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower() == "true")))
    trace.set_tracer_provider(provider)
    return provider


def build_app():
    if os.getenv("DEPLOYMENT_ENVIRONMENT") != "sandbox" or os.getenv("POD_NAMESPACE") != SANDBOX_NAMESPACE:
        raise RuntimeError("Module D entrypoint can run only in the dedicated sandbox namespace")
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        raise RuntimeError("Module D requires tracing enabled")
    service = os.environ["MODULE_D_SERVICE_NAME"]
    role = os.getenv("MODULE_D_ROLE", "observer")
    if role not in {"payments", "data", "observer"}:
        raise ValueError("MODULE_D_ROLE must be payments, data or observer")
    routes = RoutePolicy(os.environ["MODULE_D_ROUTES"])
    app = load_real_app(os.environ["MODULE_D_APP"])
    owned_provider = ensure_tracing(service)
    wrapper = ChaosMiddleware(app, service, routes, control=ControlReader(role=role))
    if owned_provider is None:
        return wrapper

    async def with_owned_lifecycle(scope, receive, send):
        try:
            return await wrapper(scope, receive, send)
        finally:
            if scope["type"] == "lifespan":
                owned_provider.shutdown()
    return with_owned_lifecycle


app = build_app()
