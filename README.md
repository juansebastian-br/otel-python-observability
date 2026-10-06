# Python OpenTelemetry Observability Lab

## Objetivo

Implementación de dos microservicios Python instrumentados con OpenTelemetry:

- `orders-service`: API de órdenes.
- `payments-service`: API de pagos.
- PostgreSQL: persistencia.
- OTel Collector: recepción OTLP, procesamiento y routing.
- Jaeger: trazas.
- Prometheus: métricas.
- CloudWatch Logs: logs en AWS.
- JSON estructurado: logs con `trace_id` y `span_id`.

## Arquitectura

```text
                         +-----------------------+
                         |   orders-service      |
Client ---> HTTP --------| FastAPI + OTel SDK    |
                         | HTTPX + SQLAlchemy    |
                         +-----------+-----------+
                                     |
                           W3C Trace Context
                                     |
                         +-----------v-----------+
                         |   payments-service    |
                         | FastAPI + OTel SDK    |
                         +-----------+-----------+
                                     |
                                     v
                              PostgreSQL

orders/payments
      | OTLP gRPC
      v
+------------------------------------------------+
|              OpenTelemetry Collector           |
| OTLP gRPC :4317 / HTTP :4318                  |
| memory_limiter -> resource -> batch            |
+----------------+-------------+-----------------+
                 |             |
              traces        metrics
                 |             |
                 v             v
              Jaeger       Prometheus
                 |
               logs
                 v
           CloudWatch Logs
```

## Fase 1

El SDK instrumenta automáticamente:

- HTTP server FastAPI.
- HTTP client HTTPX.
- SQLAlchemy/PostgreSQL.

Se agregan spans manuales para:

- `order.create`
- `payment.authorize`

Se emiten:

- trazas por OTLP/gRPC;
- métricas por OTLP/gRPC;
- logs OTLP con el mensaje como JSON estructurado y salida stdout igualmente JSON.

Los `trace_id` y `span_id` se obtienen del contexto W3C activo.

## Fase 2

El Collector recibe OTLP por:

- gRPC: `4317`
- HTTP/protobuf: `4318`

Procesamiento:

1. `memory_limiter`
2. `resource`
3. `batch`

Routing:

- traces -> Jaeger por OTLP/gRPC;
- metrics -> Prometheus exporter (`:8889/metrics`);
- logs -> AWS CloudWatch Logs exporter.

Para desarrollo local, `docker-compose.yml` usa `otel-collector-local.yaml`, que sustituye CloudWatch por `debug` para no exigir credenciales AWS.

Para AWS, use `otel-collector/otel-collector.yaml` y proporcione credenciales mediante el mecanismo IAM recomendado para el runtime (idealmente IAM Role/Task Role, no claves estáticas).

## Ejecución local

```bash
docker compose up --build
```

Generar una transacción:

```bash
curl -X POST http://localhost:8000/orders   -H 'Content-Type: application/json'   -d '{"customer_id":"customer-001","amount":125000,"currency":"COP"}'
```

Interfaces:

- Orders: http://localhost:8000
- Payments: http://localhost:8001
- Jaeger: http://localhost:16686
- Prometheus: http://localhost:9090
- Collector health: http://localhost:13133

## Consultar métricas

Prometheus scrapea el endpoint:

```text
http://otel-collector:8889/metrics
```

Ejemplos:

- `orders_created`
- `orders_create_duration`
- `payments_authorized`
- `payments_authorize_duration`

## Overhead

La configuración usa BatchSpanProcessor/BatchLogRecordProcessor y exportación periódica de métricas para reducir operaciones síncronas de red. El `memory_limiter` evita que el Collector consuma memoria indefinidamente y `batch` reduce llamadas al backend.

No se debe afirmar un porcentaje fijo de overhead sin benchmark. El reporte propone comparar:

- baseline sin OTel;
- auto-instrumentación;
- auto + custom spans + logs + metrics;
- carga baja/media/alta.

Métricas recomendadas:

- p50/p95/p99 de latencia;
- requests/s;
- CPU;
- RSS/memoria;
- bytes de red;
- cantidad de spans/logs;
- errores de exportación.

## Seguridad

- No incluir secretos en spans o logs.
- No registrar tokens, contraseñas ni datos sensibles.
- Usar TLS para OTLP fuera de redes confiables.
- En AWS, preferir IAM roles.
- Limitar cardinalidad de atributos métricos.
