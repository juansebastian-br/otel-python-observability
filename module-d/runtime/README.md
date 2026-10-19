# Runtime de caos de aplicación

Esta carpeta se monta o copia como el paquete Python `module_d_runtime` sobre una
copia aislada de la imagen real. El comando de arranque es
`uvicorn module_d_runtime.entrypoint:app`, con un solo worker por pod. La variable
`MODULE_D_APP=app.main:app` identifica la aplicación original. El wrapper conserva
su importación, sus rutas, su respuesta y su ciclo de vida.

## Entorno obligatorio

| Variable | Payments | Data |
| --- | --- | --- |
| `DEPLOYMENT_ENVIRONMENT` | `sandbox` | `sandbox` |
| `POD_NAMESPACE` | `observability-chaos-d` | `observability-chaos-d` |
| `MODULE_D_SERVICE_NAME` | `module-d-payments` | `module-d-data` |
| `MODULE_D_ROLE` | `payments` | `data` |
| `MODULE_D_ROUTES` | `{"POST":["/payments"]}` | `{"GET":["/data/health"]}` |
| `MODULE_D_APP` | `app.main:app` | `app.main:app` |

El namespace debe venir del Downward API. El entrypoint rechaza arrancar fuera de
este entorno. Se reutiliza el SDK OTel que configura la aplicación; si no existe
un proveedor global, se requiere `OTEL_EXPORTER_OTLP_ENDPOINT` para configurar
uno. No se permite `OTEL_SDK_DISABLED=true`. Las imágenes deben contener las
dependencias OTel de la aplicación, SDK y exportador OTLP gRPC; este runtime no
añade dependencias de métricas, proxies ni privilegios de red.

## Control local y apagado

No existe endpoint de activación. El único control es el archivo
`/var/run/chaos/control.json`, de máximo 4096 bytes, leído antes de cada petición
de negocio. El estado inicial y el rollback son `{"enabled":false}`. También
desactivan la inyección un archivo ausente, malformado, vencido o incompatible.

Un control activo requiere **todos y solo** estos campos:

| Campo | Validación |
| --- | --- |
| `enabled` | Booleano `true` |
| `experiment_id` | Identificador único, 1–96 caracteres alfanuméricos, `_`, `-` o `.` |
| `mode` | `latency` para payments; `error` para data |
| `issued_at` | Epoch UTC numérico, finito, no futuro |
| `expires_at` | Epoch UTC numérico posterior a `issued_at`, máximo 180 segundos después |
| `delay_ms` | Exactamente 200 para latency; 0 para error |
| `error_rate` | Exactamente 0 para latency; 0.10 para error |

No se aceptan booleanos como números, claves duplicadas, NaN, Infinity ni campos
adicionales. Un identificador ya aceptado no puede cambiar ni extenderse dentro
del proceso. Un plazo monotónico impide que un ajuste hacia atrás del reloj
prolongue un control ya observado. Las 256 primeras identidades se conservan;
si se agota ese límite, el proceso deja de aceptar identidades nuevas.

El orquestador debe escribir el JSON completo de forma atómica. Un volumen
ConfigMap puede introducir demora de propagación; la duración efectiva siempre
termina en `expires_at`, sin compensar esa demora. La expiración se comprueba al
admitir la petición; una espera de 200 ms que ya comenzó termina normalmente.
El rollback deja de afectar peticiones nuevas y no cancela operaciones de la
aplicación que ya comenzaron.

## Efecto y alcance

En payments, cada petición elegible espera 200 ms antes de entrar a la app. En
data, cada petición elegible tiene probabilidad independiente de 0.10 de recibir
HTTP 503 **antes de leer el body o invocar la aplicación**. La proporción medida
en una muestra finita puede ser distinta de 10%; debe reportarse con su tamaño
de muestra. Las peticiones no seleccionadas siguen la aplicación original.

Solo se incluyen pares método/ruta exactos configurados. Los prefijos reservados
`/health`, `/healthz`, `/ready`, `/readyz`, `/live`, `/livez`, `/docs`, `/redoc`,
`/openapi.json`, `/metrics` y `/__module_d` quedan excluidos. `/data/health` es una
ruta de negocio explícita, puesto que ejecuta comprobaciones de base de datos.
Los parámetros de consulta no alteran la ruta elegible y no aparecen en métricas
ni eventos. El wrapper no modifica credenciales, conexiones o consultas SQL;
la copia de la aplicación debe apuntar únicamente a sus dependencias sandbox.

## Evidencia y métricas

Cada efecto emite a stdout un JSON `event=chaos.applied` con `timestamp` UTC a
nanosegundos, `observed_unix_ns`, `experiment_id`, `service_name`, `namespace`,
`route`, `method`, `mode`, `delay_ms`, `error_rate`, `trace_id` y `span_id` reales.
No se registran bodies, credenciales ni query strings. Una respuesta inyectada
genera un span SERVER; una petición que llega a FastAPI conserva el SERVER de
la aplicación y un INTERNAL `module-d.guard` que incluye la espera. El header
`x-trace-id` permite correlacionar la respuesta con esa traza.

`GET /__module_d/metrics` expone Prometheus sin autenticación; mantenerlo solo
en ClusterIP y restringir el acceso mediante la política de red del sandbox.
El endpoint no cuenta como tráfico de negocio. Las métricas son:

- `module_d_http_requests_total{service,route,method,status_code}`.
- `module_d_http_request_duration_seconds_bucket{service,route,method,le}`.
- `module_d_http_request_duration_seconds_sum{service,route,method}`.
- `module_d_http_request_duration_seconds_count{service,route,method}`.
- `module_d_chaos_applied_total{service,route,mode}`.

El histograma usa límites 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, **0.2**, 0.25,
0.3, 0.5, 1, 2, 5 y +Inf segundos. Incluye la espera inyectada y la respuesta
completa de la aplicación; no incluye la red externa al proceso. Una excepción
antes de responder se contabiliza como 500 y se propaga sin ocultarse. Una
cancelación se registra como 499. Los contadores son locales al proceso y se
reinician con el pod: usar un worker por pod, un scrape por pod y `rate`/`increase`
para tolerar reinicios. Antes de la primera petición no existen series de negocio.

El primer `chaos.applied` delimita el efecto observado, **no** la hora de cambiar
el archivo. El MTTD se calcula hasta la primera alerta `firing` observada o su
primera recepción validada. No usar `startsAt` de Alertmanager como instante de
firing: puede coincidir con el inicio del estado pending.

## Validación local

Desde la raíz del repositorio, con las dependencias originales de FastAPI/OTel:

```sh
python -m unittest discover -s tests/module_d -p 'test_runtime*.py' -v
```

Las pruebas cubren límites de entorno/tiempo/roles/rutas, configuración inválida,
retorno 503 sin ejecutar negocio, espera medida de 200 ms, métricas acumulativas,
traza real y propagación W3C, una sola traza SERVER con FastAPI instrumentado,
fallos de la aplicación y preservación del ciclo de vida. No despliegan ni
ejecutan experimentos contra recursos de nube.
