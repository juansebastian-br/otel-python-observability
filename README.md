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
- Grafana: http://localhost:3000 (dashboard "OTel Python Observability · SLIs")
- Collector health: http://localhost:13133

Generar carga (300 órdenes, 5 en paralelo):

```bash
scripts/load.sh
```

`payments-service` falla a propósito un 2% de los pagos (`PAYMENTS_FAILURE_RATE`) para que los paneles de error tengan datos. Para el benchmark use `PAYMENTS_FAILURE_RATE=0 docker compose up -d`.

## Despliegue en GCP (GKE Autopilot)

Requisitos: `gcloud` autenticado (`gcloud auth login`), `kubectl`, Docker con `buildx` y un proyecto GCP con facturación.

```bash
PROJECT_ID=<proyecto> REGION=us-central1 scripts/deploy-gke.sh   # build, push, cluster, IAM, manifiestos
scripts/port-forward-gke.sh                                       # UIs en localhost (dejar abierto)
scripts/load.sh                                                   # en otra terminal
PROJECT_ID=<proyecto> scripts/destroy-gke.sh                      # al terminar, para no seguir pagando
```

Contraseña de Grafana (usuario `admin`):

```bash
kubectl -n observability get secret grafana-credentials -o jsonpath='{.data.password}' | base64 -d; echo
```

| Recurso | Archivo | Notas |
|---|---|---|
| orders / payments | `k8s/orders.yaml`, `k8s/payments.yaml` | imágenes en Artifact Registry `otel-lab` |
| PostgreSQL | `k8s/postgres.yaml` | `emptyDir`, credenciales en Secret `postgres-credentials` |
| OTel Collector | `k8s/otel-collector.yaml` + `otel-collector/otel-collector-gke.yaml` | logs → Cloud Logging vía Workload Identity (`roles/logging.logWriter`) |
| Jaeger v2 | `k8s/jaeger.yaml` | almacenamiento en memoria |
| Prometheus | `k8s/prometheus.yaml` + `prometheus/prometheus.yml` | scrapea `:8889` (apps) y `:8888` (Collector) |
| Grafana | `k8s/grafana.yaml` + `grafana/` | datasources y dashboard provisionados |

En GKE los logs quedan en Cloud Logging → Logs Explorer con `logName="projects/<proyecto>/logs/otel-python-observability"`.

## Fase 3 — Backends y visualización

### Trazas y propagación de contexto

HTTPX inyecta el header W3C `traceparent` y FastAPI lo extrae en `payments-service`, de modo que una orden produce **una sola traza** que cruza ambos servicios. En Jaeger (servicio `orders-service`, operación `POST /orders`):

```text
- [orders-service]   POST /orders
  - [orders-service]   order.create
    - [orders-service]   INSERT observability
    - [orders-service]   POST                      (cliente HTTPX)
      - [payments-service] POST /payments
        - [payments-service] payment.authorize
          - [payments-service] INSERT observability
    - [orders-service]   UPDATE observability
```

El `trace_id` de la traza es el mismo que aparece en los logs JSON de ambos servicios (`docker compose logs payments-service | grep <trace_id>`).

### Dashboard Grafana (6 paneles)

Definido en `grafana/dashboards/otel-slis.json`. Las métricas HTTP usan la semántica estable (`OTEL_SEMCONV_STABILITY_OPT_IN=http`).

| # | Panel | Métrica base |
|---|---|---|
| 1 | SLI Tráfico (req/s) | `http_server_request_duration_seconds_count` |
| 2 | SLI Tasa de errores 5xx (%) | `..._count{http_response_status_code=~"5.."}` / total |
| 3 | SLI Latencia p50/p95/p99 | `http_server_request_duration_seconds_bucket` |
| 4 | SLI Disponibilidad (%) | `1 - 5xx/total` en el rango del dashboard (SLO 99.5%) |
| 5 | CPU | `process_cpu_time_seconds_total` (servicios) y `otelcol_process_cpu_seconds` (Collector) |
| 6 | Errores del OTel Collector | `otelcol_exporter_send_failed_*`, `otelcol_receiver_refused_*`, `otelcol_receiver_failed_*`, `otelcol_exporter_queue_size` |

Para demostrar el panel 6, detenga Jaeger mientras genera carga (`docker compose stop jaeger` o `kubectl -n observability scale deploy/jaeger --replicas=0`). `otelcol_exporter_queue_size` sube de inmediato. `send_failed` solo aumenta cuando se agotan los reintentos (~5 min).

## Consultar métricas

Prometheus scrapea `otel-collector:8889/metrics` (telemetría de las apps) y `otel-collector:8888/metrics` (telemetría interna del Collector).

Métricas de negocio:

- `orders_created_total`
- `orders_create_duration_milliseconds_bucket`
- `payments_authorized_total`
- `payments_authorize_duration_milliseconds_bucket`

Métricas de proceso (para el análisis de overhead): `process_cpu_time_seconds_total`, `process_cpu_utilization_ratio`, `process_memory_usage_bytes`.

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
