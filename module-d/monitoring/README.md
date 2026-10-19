# Monitorización aislada del Módulo D

Prometheus 3.7.2, Alertmanager 0.28.1 y un receptor HTTP de evidencia se ejecutan únicamente en `observability-chaos-d`. No necesitan IAM, RBAC, descubrimiento de Kubernetes ni acceso a credenciales. Los tres Services son ClusterIP; el acceso desde Cloud Shell se hace mediante port-forward. No se cambian políticas, Collector, dashboard ni aplicaciones de C/B.

`render.py` solo genera configuración local. `build(config)` es una función pura para el instalador principal; devuelve `manifest`, `prometheus`, `alertmanager`, `rules` y `queries`. El manifiesto incluye tres ConfigMaps, tres Deployments, tres Services y una NetworkPolicy que permite acceso entrante a los pods de monitorización solo desde el namespace D. El instalador debe comprobar propiedad antes de aplicar y complementar las políticas de egreso del sandbox: Prometheus necesita acceder a pagos y datos; Alertmanager al receptor; ambos requieren DNS.

## Contrato acordado y generación

El wrapper expone `/__module_d/metrics` en el mismo puerto que la aplicación. Las métricas usan etiquetas `service`, `route`, `method`, `status_code`:

- `module_d_http_requests_total` es un contador.
- `module_d_http_request_duration_seconds_bucket` es el histograma de duración; tiene también `_sum` y `_count`.
- La identidad de métricas es `module-d-payments` para `POST /payments` y `module-d-data` para `GET /data/health`. Esta última es la ruta real confirmada del servicio; no confundirla con `/health`, excluida de las mediciones del wrapper.
- `experiment_id` y `trace_id` permanecen en los eventos JSON; no se convierten en etiquetas de alta cardinalidad.

Antes de la línea base, declarar el objetivo de latencia y usar el digest verificado de una imagen Python aprobada para el receptor. No copiar una etiqueta mutable ni una imagen inventada. El instalador principal puede pasar directamente el mismo digest de la copia autorizada de payments.

```bash
python3 module-d/monitoring/render.py \
  --namespace observability-chaos-d \
  --receiver-image "$D_RECEIVER_IMAGE" \
  --latency-threshold-ms 200 --minimum-samples 20 \
  --payments-service module-d-payments --payments-route /payments \
  --payments-method POST --payments-target module-d-payments:8001 \
  --data-service module-d-data --data-route /data/health \
  --data-method GET --data-target module-d-data:8002 \
  --out module-d/.state/monitoring-contract
```

`D_RECEIVER_IMAGE` debe contener el digest real identificado por el instalador, con formato `registro/imagen@sha256:...`. El archivo `contract.json` guarda parámetros, SLO de disponibilidad 99.5 %, objetivo de latencia 99 %, fecha de declaración y estado **solo configurado**. Si cambia cualquier parámetro, el generador exige otra carpeta: declarar de nuevo y repetir la línea base, sin reescribir el contrato de un ensayo ya medido.

Los archivos JSON son también YAML válido y se montan como `prometheus.yaml`, `rules.yaml` y `alertmanager.yaml`. Los init containers ejecutan `promtool check config` y `amtool check-config` con las mismas versiones de los servidores. No iniciar experimentos si fallan esos controles, no aparecen dos targets UP o la línea base viola el umbral predeclarado.

## Alertas y límites de la medición

Scrape cada 5 s, timeout 4 s, evaluación cada 5 s, ventana de 60 s y retención de 30 s. Ambas condiciones exigen al menos 20 solicitudes observadas en la ventana y filtran servicio, ruta y método exactos:

1. **ModuleDLatency:** p99 de payments superior al umbral predeclarado, 0.2 s cuando se declara 200 ms. No depende de que existan errores.
2. **ModuleDErrorRate:** proporción 5xx de data superior a 0.005. No depende de una elevación de latencia.
3. **ModuleDScrapeUnavailable:** un target no responde durante 30 s. Esta alerta de salud de telemetría no demuestra detección del caos.

El p99 se estima desde buckets. Para calcular el presupuesto de latencia, usar el conteo de solicitudes bajo/sobre el umbral de la ventana, no convertir directamente el p99 en un porcentaje. Los timeouts del cliente deben incluirse en el SLI del generador: un request que nunca finaliza puede no producir un contador de respuesta HTTP en el servidor. No presentar la regla de 5xx como cobertura de todo tipo de timeout.

La retención y los intervalos buscan una detección inferior a 120 s, pero **no la garantizan**. Se mide por ensayo desde el primer evento real `chaos.applied`. No utilizar como instante de detección `activeAt` de Prometheus ni `startsAt` de Alertmanager: pueden reflejar el comienzo del estado pendiente, antes de los 30 s de retención.

El receptor registra `received_at` en UTC al recibir la notificación y devuelve `/events` con los últimos 1000 eventos de memoria. También escribe JSON por stdout, capturable con `kubectl logs` y Cloud Logging. Guardar los eventos antes de borrar/reiniciar el pod. Las notificaciones repetidas no son nuevos incidentes; conservar la primera recepción firing por fingerprint/inicio del episodio. La notificación contiene servicio, ruta, valor, umbral, enlace a la consulta de Prometheus y procedimiento de reversión. No envía correos ni mensajes externos.

## Capturar los tiempos reales

Después de aplicar mediante el instalador con sus verificaciones de propiedad, abrir tres terminales de Cloud Shell:

```bash
kubectl -n observability-chaos-d port-forward svc/module-d-prometheus 19090:9090
kubectl -n observability-chaos-d port-forward svc/module-d-alertmanager 19093:9093
kubectl -n observability-chaos-d port-forward svc/module-d-alert-receiver 18080:8080
```

Antes de activar el fallo, iniciar la observación en otra terminal:

```bash
python3 module-d/monitoring/capture.py \
  --out module-d/.state/observacion-ensayo-1 --seconds 600 --poll-seconds 2
```

Para el otro ensayo elegir otra carpeta. `capture.py` solo acepta URLs loopback y no inyecta datos, caos ni alertas. Conserva targets/reglas, cada sondeo y el primer momento en que observa estado `firing`; consulta la API v2 de Alertmanager y la API del receptor. Ante errores de API registra el fallo, sin tratarlo como salud. Guarda `mttd_seconds=null`: el análisis debe correlacionar con el primer evento de inyección del servidor.

Usar como evidencia diferenciada: `first_observed_firing_at - t0` (cota superior de observación, con sondeo de 2 s) y `received_at - t0` (notificación interna realmente recibida). Conservar el primer `chaos.applied` UTC y traza del pod, la identidad del episodio y su cierre. Una alerta de otro servicio, ruta o ensayo no satisface el objetivo. El webhook no acredita recepción de email.

Los datos de Prometheus y Alertmanager usan `emptyDir`: son efímeros. Exportar toda la evidencia antes de limpiar. No se ha ejecutado ningún experimento por generar estos archivos o aprobar pruebas unitarias.

## Validación y fuentes primarias

```bash
python3 -m unittest discover -s tests/module_d -p 'test_monitoring*.py' -v
```

- [Configuración de Prometheus](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).
- [Estados y retención de reglas de alerta](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).
- [Configuración de Alertmanager y webhooks](https://prometheus.io/docs/alerting/latest/configuration/).
- [API de Prometheus](https://prometheus.io/docs/prometheus/latest/querying/api/).
- [Seguridad de GKE Autopilot](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-security).
- [Prometheus 3.7.2](https://github.com/prometheus/prometheus/releases/tag/v3.7.2) y [Alertmanager 0.28.1](https://github.com/prometheus/alertmanager/releases/tag/v0.28.1).
