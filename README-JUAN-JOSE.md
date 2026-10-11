# Aporte de Juan José

Este aporte implementa la correlación de logs y trazas en Grafana Explore y el experimento de overhead. Parte del commit `e1d234c` del repositorio del equipo. El laboratorio se ejecuta localmente; los resultados de este experimento no representan mediciones en GKE ni ECS.

## Resultado ejecutado y entrega

La ejecución real del 11 de octubre de 2026 está conservada en [docs/evidencias/20261011T230053Z](docs/evidencias/20261011T230053Z). Se usaron 50 usuarios durante cinco minutos por escenario, con 30 segundos de calentamiento previo y cero errores HTTP en ambos escenarios.

| Medida | Baseline | OTel | Diferencia |
|---|---:|---:|---:|
| p99 HTTP (ms) | 164,65 | 255,31 | +90,65 ms |
| CPU media de las aplicaciones (% de un núcleo) | 44,46 | 61,86 | +39,16 % relativo |
| Memoria media de las aplicaciones (MiB) | 323,14 | 223,36 | −99,78 MiB |
| Memoria media del laboratorio completo (MiB) | 653,63 | 876,63 | +223,00 MiB |

Es una sola pareja exploratoria. La reducción de memoria de las aplicaciones no demuestra un ahorro general atribuible a OTel; el orden, las cachés y el asignador pueden influir. La memoria total incluye los ocho contenedores y la acumulación de datos en los backends.

El [informe integrado de 15 páginas](docs/Informe_Juan_Jose.pdf) conserva el documento del equipo y añade este aporte en las páginas 10–15. La correlación pasó las verificaciones de contexto, spans, logs y configuración de Grafana vía API. La captura visual nueva de Grafana/Jaeger quedó pendiente por una restricción del navegador; el gráfico de spans del informe procede de datos reales de Jaeger y no se presenta como captura de interfaz.

Lea la [nota de ejecución](docs/evidencias/20261011T230053Z/NOTA-EJECUCION.md) para conocer el ajuste de espera de arranque aplicado después de las mediciones y la verificación final. También se incluye el [Word editable](docs/Informe_Juan_Jose.docx). El historial Git permite identificar la publicación de este aporte; este paquete no acredita un despliegue nuevo en GKE/ECS.

## Ejecutar el laboratorio y el benchmark

Requisitos: Docker Desktop con contenedores Linux, Docker Compose 2.24.4 o posterior (soporte de `!override`), Python 3.10 o posterior y k6. El controlador del benchmark solo usa la biblioteca estándar de Python.

Desde la raíz del repositorio:

```powershell
python benchmark/run.py
```

El controlador:

1. Levanta el proyecto aislado `otel-juan-jose`, con su propia base de datos y puertos.
2. Envía una orden con un `traceparent` W3C válido y comprueba Loki y Jaeger.
3. Reinicia ambos servicios con `OTEL_SDK_DISABLED=true`.
4. Calienta durante 30 segundos y mide 50 VUs durante 300 segundos.
5. Repite con `OTEL_SDK_DISABLED=false`, la misma carga y los mismos límites de recursos.
6. Exporta los datos originales y la comparación a `benchmark/results/<fecha UTC>/`.
7. Restaura la instrumentación y vuelve a verificar la correlación.

El reseteo de tablas afecta exclusivamente al PostgreSQL del proyecto `otel-juan-jose`. No use ese proyecto para datos de trabajo ajenos al benchmark. Un fallo de calentamiento detiene la prueba; un fallo de carga o muestreo impide presentar el resultado como válido.

Para tres parejas alternando el orden A/B, B/A, A/B:

```powershell
python benchmark/run.py --vus 50 --repetitions 3
```

Los experimentos usan 50-100 usuarios, cinco minutos por escenario, cero fallos artificiales y muestreo de trazas `always_on`. `sleep(0.5 + Math.random())` modela un tiempo de reflexión entre peticiones de 0.5 a 1.5 segundos, con semilla fija. La misma imagen mantiene los imports OTel y llamadas no-op en baseline: se mide el coste de la instrumentación activa y exportación, no el ahorro de eliminar físicamente todas las dependencias.

## Correlación de logs y trazas

Flujo implementado:

```text
POST /orders -> orders-service -> HTTPX -> payments-service -> PostgreSQL
                       | W3C traceparent conserva trace_id |
                       +---------------+-------------------+
                                       | OTLP/gRPC
                                 OTel Collector
                                  /          \
                        trazas OTLP           logs OTLP/HTTP
                              /                \
                           Jaeger              Loki
                              \                /
                                Grafana Explore
```

`JsonFormatter` escribe `trace_id` de 32 dígitos hexadecimales y `span_id` de 16 dígitos. Se aplica tanto a consola como al handler OTLP, cuyo LogRecord también conserva sus identificadores nativos. Fuera de un span activo, los identificadores JSON son `null`, no identificadores ficticios.

Loki recibe OTLP nativo en `/otlp/v1/logs`. Solo `service.name` y `deployment.environment.name` se promueven a etiquetas de índice. El `trace_id` se consulta como metadato estructurado, evitando crear un stream por petición.

Grafana y Jaeger están en:

- Grafana: http://localhost:13000
- Jaeger: http://localhost:16687
- Prometheus: http://localhost:19090

En Grafana, abra **Explore**, seleccione **Loki**, cambie a modo **Code** y consulte:

```logql
{service_name=~"orders-service|payments-service"}
```

Para una petición específica, copie el identificador de `verification.json`:

```logql
{service_name=~"orders-service|payments-service"} | trace_id="IDENTIFICADOR_REAL"
```

Expanda una línea de negocio. El campo derivado **TraceID** muestra **Ver traza en Jaeger** y abre la traza en otra vista de Explore. Compruebe `order.create`, la llamada HTTP, `payment.authorize` y los spans SQL. El enlace inverso configurado en Jaeger filtra los logs por el identificador de la traza, con margen temporal de un minuto. El archivo `correlation/abrir-grafana.url` abre directamente la consulta y su ventana temporal.

La verificación automatizada exige el mismo `trace_id`, que cada `span_id` de log exista en Jaeger, el mismo `order_id`, ambos servicios, spans SQL y al menos una relación `CHILD_OF` que cruce de pedidos a pagos. Conserva `loki.json`, `jaeger.json` y `verification.json`. Estos archivos son evidencia de backend; no sustituyen una captura que pruebe el clic en la interfaz.

Las métricas de Prometheus agregan muchas peticiones. No se añade `trace_id` como etiqueta de cada serie: eso produce alta cardinalidad. La correlación métrica-traza por exemplars pertenece al alcance de instrumentación/métricas del equipo y no se afirma como verificada en este aporte.

## Medidas y fórmulas

El cliente k6 mide `http_req_duration` (envío, espera y recepción; excluye establecimiento de conexión). El p99 es el percentil de las peticiones del escenario; no el percentil de las medias. Se exportan también p95, solicitudes, req/s y fracción de errores.

CPU y memoria se miden externamente con `docker stats`, usando exactamente el mismo método en ambas variantes. Se muestrea cada aproximadamente cinco segundos (duración de `docker stats --no-stream` más una espera de tres segundos). CPU media y memoria media suman los contenedores de pedidos y pagos. Docker CPU 100% equivale a un núcleo lógico; puede superar 100%. La memoria es el uso del contenedor mostrado por Docker, excluyendo caché en Linux; no se etiqueta como RSS del proceso.

```text
Latencia adicional p99 (ms) = p99_OTel - p99_baseline
Overhead de latencia (%)    = 100 * (p99_OTel / p99_baseline - 1)
Overhead CPU relativo (%)   = 100 * (CPU_OTel / CPU_baseline - 1)
Diferencia CPU (p.p.)       = CPU_OTel - CPU_baseline
Memoria adicional (MiB)     = memoria_OTel - memoria_baseline
```

Si CPU baseline es cero, el overhead relativo se informa como no definido. La memoria pico es el mayor valor de la suma simultánea de ambos contenedores, no la suma de máximos ocurridos en momentos distintos. Los recursos incluyen hasta 15 segundos de drenaje de iteraciones, explícitos en `run.json`. El Collector se informa por separado, sin sumar su coste al de las aplicaciones de forma inadvertida.

## Archivos de evidencia

| Archivo | Contenido |
|---|---|
| `metadata.json` | Entorno, versiones, carga, duración, muestreo, base Git y orden de ejecución |
| `01-baseline/summary.json` y `01-otel/summary.json` | Agregados originales de k6 |
| `resources.csv` por escenario | CPU y memoria por contenedor y tiempo |
| `run.json` por escenario | Ventanas de ejecución, código de salida y errores de muestreo |
| `collector.log` por escenario | Diagnósticos recientes del Collector |
| `comparacion.csv`, `comparacion.json`, `comparacion.md` | Tabla y diferencias calculadas |
| `correlation*/` | Respuestas reales de Loki y Jaeger, validación W3C y enlace a Explore |

Una pareja es una medición exploratoria, sin intervalo de confianza. Las diferencias pueden depender de la contención del equipo, las cachés, la frecuencia de CPU y el orden. Tres o más parejas alternadas mejoran la evaluación. El controlador reporta la mediana de los p99 por corrida, nunca la presenta como un p99 global combinado. Una caída de req/s en este modelo cerrado debe analizarse junto con el aumento de latencia.

## Integración con el despliegue GKE y ECS del equipo

El overlay local no cambia el despliegue cloud existente. Para usar Loki junto con los exportadores cloud, incorpore estas dos partes en las configuraciones que el equipo despliega:

```yaml
exporters:
  otlphttp/loki:
    endpoint: http://loki:3100/otlp

# En service.pipelines.logs:
# GKE: exporters: [googlecloud, otlphttp/loki]
# ECS: exporters: [awscloudwatchlogs, otlphttp/loki]
```

La dirección debe ser resoluble desde el Collector; en ECS se adapta al descubrimiento privado del servicio. Despliegue Loki con almacenamiento persistente y acceso privado, y aprovisione el datasource de `grafana/juan-jose/datasources.yaml`. Conserve IAM/Workload Identity y las salidas a Cloud Logging/CloudWatch. La configuración filesystem de este laboratorio es de demostración, no de alta disponibilidad. No hay una nueva ejecución cloud ni manifiestos ECS validados en este aporte.

Para un benchmark cloud, utilice dos despliegues de prueba con la misma imagen, réplicas y recursos; fije el autoescalado durante cada pareja. Mida CPU y memoria con una fuente externa común en ambos escenarios (métricas de contenedores/GKE o Container Insights/ECS), y ejecute el generador en una máquina separada. No use las métricas del SDK como fuente de consumo del baseline, porque el SDK está apagado. Registre proveedor, región, tipo de máquina y ventanas exactas por separado.

## Detener sin borrar evidencia

```powershell
docker compose -p otel-juan-jose -f docker-compose.yml -f compose.juan-jose.yaml stop
```

Loki conserva su volumen y los archivos del benchmark permanecen en disco. Jaeger usa memoria: exporte la evidencia antes de recrearlo.

## Fuentes técnicas

- W3C Trace Context: https://www.w3.org/TR/trace-context/
- Loki y OTLP: https://grafana.com/docs/loki/latest/send-data/otel/
- Campos derivados de Loki: https://grafana.com/docs/grafana/latest/datasources/loki/configure/
- Integración Jaeger en Grafana: https://grafana.com/docs/grafana/latest/datasources/jaeger/configure/
- Métricas k6: https://grafana.com/docs/k6/latest/using-k6/metrics/reference/
- Estadísticas Docker: https://docs.docker.com/reference/cli/docker/container/stats/
