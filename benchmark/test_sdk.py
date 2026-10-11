"""Fast, isolated checks; these are not benchmark results or backend evidence."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def child(service, disabled):
    os.environ['OTEL_SDK_DISABLED']=str(disabled).lower()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry._logs import get_logger_provider
    spec=importlib.util.spec_from_file_location('tested_otel',ROOT/f'services/{service}/app/otel.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    spans=InMemorySpanExporter(); logs=InMemoryLogRecordExporter()
    m.OTLPSpanExporter=lambda **kw: spans
    m.OTLPLogExporter=lambda **kw: logs
    m.OTLPMetricExporter=lambda **kw: object()
    m.PeriodicExportingMetricReader=lambda *a,**kw: InMemoryMetricReader()
    engine=create_engine('sqlite://')
    app=FastAPI()
    tracer,meter,logger=m.configure_otel(app,engine)
    @app.get('/probe')
    def probe():
        with tracer.start_as_current_span('business.test'):
            with engine.connect() as c: c.execute(text('select 1'))
            logger.info('probe',extra={'event':'probe','order_id':'test'})
            return {'recording':trace.get_current_span().is_recording()}
    tid='1234567890abcdef1234567890abcdef'
    with TestClient(app) as client:
        r=client.get('/probe',headers={'traceparent':f'00-{tid}-1234567890abcdef-01'})
        assert r.status_code==200
        assert r.json()['recording'] is not disabled
    if disabled:
        assert not getattr(app,'_is_instrumented_by_opentelemetry',False)
        assert len(logging.getLogger().handlers)==1
        assert not spans.get_finished_spans()
        assert not logs.get_finished_logs()
    else:
        trace.get_tracer_provider().force_flush()
        get_logger_provider().force_flush()
        records=[x.log_record for x in logs.get_finished_logs() if json.loads(x.log_record.body).get('event')=='probe']
        assert len(records)==1
        body=json.loads(records[0].body)
        assert body['trace_id']==tid==format(records[0].trace_id,'032x')
        assert body['span_id']==format(records[0].span_id,'016x')
        assert any(s.name=='business.test' for s in spans.get_finished_spans())
        assert any(s.name=='select' for s in spans.get_finished_spans())
        assert body['span_id'] in {format(s.context.span_id,'016x') for s in spans.get_finished_spans()}
    m.shutdown_otel()
    print(f'PASS {service} disabled={disabled}',flush=True)

if __name__=='__main__':
    if len(sys.argv)>1: child(sys.argv[1],sys.argv[2]=='true')
    else:
        for service in ('orders','payments'):
            for flag in ('true','false'):
                subprocess.run([sys.executable,__file__,service,flag],check=True)
