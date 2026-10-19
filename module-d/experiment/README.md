# Ejecución y evidencia del Módulo D

`runner.py` genera HTTP con llegadas fijas y límites; no activa fallos. `session.py`
coordina únicamente las copias aisladas del namespace `observability-chaos-d`.
`analyze.py` calcula resultados a partir de observaciones, nunca inventa incidentes.
Todos usan la biblioteca estándar de Python 3.10 o posterior.

## Sesión formal desde Cloud Shell

Después de aplicar y revisar `module-d/gcp/deploy.py`, desde la raíz del repositorio:

```bash
python module-d/experiment/session.py --project "$GOOGLE_CLOUD_PROJECT" \
  --mode latency --out .state/d-latency-final
python module-d/experiment/session.py --project "$GOOGLE_CLOUD_PROJECT" \
  --mode error --out .state/d-error-final
```

El proyecto debe coincidir con `gcloud config get-value project` y el contexto GKE
declarado (`--location us-central1 --cluster otel-lab`, ajustables antes de empezar).
Cada directorio debe ser nuevo. Primero revise el resultado de latencia; no lance
la segunda sesión si hay fallos de aislamiento, rollback o medición sin resolver.

Cada sesión ofrece 5 solicitudes/s, con máximo 20 solicitudes simultáneas: 900 s
de baseline (4500 solicitudes), 180 s de caos (900) y 300 s de recuperación (1500).
Preflight, transiciones y captura final agregan tiempo a los 23 minutos de carga.
Las transiciones esperan solicitudes pendientes, pausan las llegadas y quedan
registradas; no se presentan como carga perdida ni se atribuyen al SLO de una fase.

Antes de activar, se verifica proyecto/contexto, propiedad del namespace y de cada
Deployment, cadena Deployment–ReplicaSet–Pod, una réplica lista y ausencia de
reinicios/cambios de identidad. Se crean túneles propios a `127.0.0.1`: 18001,18002,
19090,19093,18080,16686. Un puerto ocupado produce rechazo; no se reutiliza un túnel
desconocido. Un lock exclusivo impide sesiones simultáneas. No se elimina un lock
de una sesión interrumpida sin revisar antes los procesos, el control y la evidencia.

Preflight verifica `POST /payments` con UUID, importe 100 COP y `GET /data/health`,
dos objetivos de Prometheus UP y ambas trazas realmente ingeridas en Jaeger.
El baseline autoriza caos solamente con 4500 resultados sin errores ni llegadas
perdidas, p99 del cliente <200 ms y sin alertas pendientes/activas.

El control atómico usa el reloj del pod, TTL absoluto máximo 180 s, +200 ms para
payments o probabilidad 10% de error para data. La activación precede por segundos
al inicio de la ventana de carga; se conserva esa diferencia y no se extiende el
TTL. El tiempo de inyección efectivo se obtiene de `chaos.applied`, no del nombre
de la fase. La tasa 10% es probabilística: el porcentaje observado puede variar.
Se solicita OFF al entrar a recuperación y nuevamente en `finally`. Si falla
rollback, la evidencia lo declara; el TTL sigue siendo la barrera de expiración.

## Resultados que se conservan

- `contract.json`: objetivos fijados antes de medir: disponibilidad 99,5%, latencia
  99% de respuestas observadas ≤200 ms; baseline usa p99 <200 ms como guarda.
- `requests.jsonl`: UTC RFC3339, fase, estado HTTP/timeout, duración, solicitud
  sintética, `sent_trace_id` y `response_trace_id`. Un ID enviado o devuelto no
  demuestra por sí solo que la traza llegó a Jaeger.
- `analysis.json`: N intentadas, F resultados no 2xx/fallos de transporte, p99
  empírico, SLI y presupuesto por fase y experimento; N=0 produce desconocido.
  El presupuesto permitido es `(1-SLO)*N`; consumido es `fallos/permitido*100`.
  Es el presupuesto de esa ventana, no un presupuesto mensual.
- Las llegadas no emitidas no incrementan N; se cuentan aparte e invalidan la
  comparación de carga. Timeouts/transporte fallido no son respuestas rápidas.
  Redirecciones/4xx se preservan como fallos y como contrato inesperado.
- `first-chaos-applied.json` conserva `observed_unix_ns` del servidor. La demora
  de detección usa el primer estado `firing` observado en Prometheus; `activeAt`
  y Alertmanager `startsAt` pueden incluir pending y no se usan como detección.
  La recepción del webhook interno y su resolución se reportan por separado.
  Objetivo estricto: <120 s. Sin observación, MTTD queda desconocido; nunca cero.
- `server-metric-history.json` conserva métricas de la aplicación en ventanas
  móviles de 60 s. Su latencia excluye la red/port-forward que sí incluye el cliente.
- Trazas afectadas de Jaeger, logs de caos filtrados, captura de alertas,
  comprobaciones finales de pods y `rollback.json` completan la evidencia.

`session.json` separa carga terminada, problemas de evidencia y rollback.
`EVIDENCE_VALIDATED` no significa que el SLO o MTTD haya cumplido: revise los valores
de `analysis.json`. No se verifica email ni reconocimiento humano. `.state` es
evidencia local privada y no debe publicarse completa sin revisión.

## Runner parametrizable, sin control de caos

Para una comprobación corta sobre un túnel propio, cree un archivo JSON real con
el cuerpo de la ruta y ejecute, por ejemplo, la ruta observada de data:

```bash
printf '{}\n' > .state/data-body.json
python module-d/experiment/runner.py \
  --target-url http://127.0.0.1:18002 --allow-host 127.0.0.1 \
  --path /data/health --method GET --body-file .state/data-body.json \
  --experiment-id data-check --baseline-seconds 10 --chaos-seconds 0 \
  --recovery-seconds 0 --output .state/data-check.jsonl
python module-d/experiment/analyze.py --events .state/data-check.jsonl \
  --experiment-id data-check --latency-threshold-ms 200 \
  --output .state/data-check-analysis.json
```

Solo se aceptan loopback o nombres de servicio del namespace dedicado, con host
explícitamente permitido, puerto explícito y ruta sin credenciales/query. No se
siguen redirecciones ni proxies. Si una ruta requiere secreto, use
`--header-env Authorization=MODULE_D_AUTHORIZATION` con la variable previamente
configurada: cuerpo, cabeceras y texto de excepciones no se guardan en JSONL.

## Pruebas locales

```bash
python -m unittest discover -s tests/module_d -p 'test_experiment*.py' -v
```

Los tests HTTP levantan un servidor temporal exclusivamente en loopback; no
contactan GCP, no activan caos y no necesitan credenciales. Si un sandbox bloquea
loopback con `PermissionError`, eso es una limitación del entorno de prueba.
