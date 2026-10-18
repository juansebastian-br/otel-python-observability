"""Independent sandbox workload: authentication telemetry without a database.

Deploy separately from the team's orders/payments/data services. This is an
explicit demonstration scope and does not claim to secure those live services.
"""
from contextlib import asynccontextmanager
import os

from fastapi import Depends, FastAPI
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from services.orders.app.security import build_api_key_guard

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "module-c-auth-probe")
provider = TracerProvider(resource=Resource.create({
    "service.name": SERVICE_NAME,
    "deployment.environment.name": os.getenv("DEPLOYMENT_ENVIRONMENT", "sandbox"),
}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317"),
    insecure=os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower() == "true",
)))


@asynccontextmanager
async def lifespan(app):
    yield
    provider.shutdown()


app = FastAPI(title="Module C sandbox authentication probe", lifespan=lifespan)
require_api_key = build_api_key_guard()


@app.get("/health")
async def health():
    return {"status": "UP", "service": SERVICE_NAME}


@app.post("/orders", dependencies=[Depends(require_api_key)])
async def authentication_probe():
    return {"status": "authenticated", "service": SERVICE_NAME}


FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
