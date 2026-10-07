import json
import logging
import os
import socket

from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "orders-service")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
ENVIRONMENT = os.getenv("DEPLOYMENT_ENVIRONMENT", "local")

resource = Resource.create({
    "service.name": SERVICE_NAME,
    "service.version": os.getenv("SERVICE_VERSION", "1.0.0"),
    "deployment.environment.name": ENVIRONMENT,
    "service.instance.id": socket.gethostname(),
})

def configure_otel(app, engine):
    # Traces
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)
        )
    )
    trace.set_tracer_provider(tracer_provider)

    # Metrics -> OTLP -> Collector -> Prometheus exporter
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
        export_interval_millis=10000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # Process CPU/memory metrics (dashboard CPU panel and overhead analysis).
    SystemMetricsInstrumentor(
        config={
            "process.cpu.time": ["user", "system"],
            "process.cpu.utilization": None,
            "process.memory.usage": None,
            "process.thread.count": None,
        }
    ).instrument(meter_provider=meter_provider)

    # Logs -> OTLP -> Collector -> CloudWatch
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True)
        )
    )
    set_logger_provider(logger_provider)
    otel_handler = LoggingHandler(
        level=logging.INFO,
        logger_provider=logger_provider,
    )

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            span = trace.get_current_span()
            ctx = span.get_span_context()
            trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
            span_id = format(ctx.span_id, "016x") if ctx.is_valid else None
            payload = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "service": SERVICE_NAME,
                "message": record.getMessage(),
                "trace_id": trace_id,
                "span_id": span_id,
            }
            for key in ("event", "order_id"):
                value = getattr(record, key, None)
                if value is not None:
                    payload[key] = value
            if record.exc_info:
                payload["exception"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False)

    stdout = logging.StreamHandler()
    stdout.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(stdout)
    root.addHandler(otel_handler)

    # Auto-instrumentation for HTTP and DB.
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=tracer_provider)
    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        tracer_provider=tracer_provider,
    )

    return (
        trace.get_tracer(SERVICE_NAME),
        metrics.get_meter(SERVICE_NAME),
        logging.getLogger(SERVICE_NAME),
    )

def shutdown_otel():
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    meter_provider = metrics.get_meter_provider()
    if hasattr(meter_provider, "shutdown"):
        meter_provider.shutdown()
    logger_provider = get_logger_provider()
    if hasattr(logger_provider, "shutdown"):
        logger_provider.shutdown()
