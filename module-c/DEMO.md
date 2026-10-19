# Demostración del módulo C en GCP — guion público de 6 a 8 minutos

Este documento describe un procedimiento reproducible. No contiene evidencia de una cuenta, resultados operacionales ni un video grabado. Registrar los resultados reales de cada ejecución en una entrega privada, con sus fechas y alcance.

## Preparación antes de grabar

Completar primero la instalación y revisión descritas en [la guía GCP](gcp/README.md). Trabajar desde la raíz del repositorio en Cloud Shell. El plan operacional `module-c/gcp/.state/plan.json` debe corresponder al sandbox autorizado y contener la configuración de red seleccionada; no cambiar el backend durante la grabación ni publicar ese archivo.

Obtener los valores del entorno desde el plan, sin escribir identificadores de una cuenta en este guion. Ejecutar este bloque en cada terminal que vaya a utilizar las variables `MODULE_C_*`:

```bash
readarray -t MODULE_C_CONFIG < <(python3 - <<'PY'
import json
from pathlib import Path
config = json.loads(Path('module-c/gcp/.state/plan.json').read_text())['config']
for field in ('project', 'cluster', 'location', 'namespace'):
    print(config[field])
PY
)
MODULE_C_PROJECT="${MODULE_C_CONFIG[0]}"
MODULE_C_CLUSTER="${MODULE_C_CONFIG[1]}"
MODULE_C_LOCATION="${MODULE_C_CONFIG[2]}"
MODULE_C_NAMESPACE="${MODULE_C_CONFIG[3]}"
MODULE_C_CONTEXT="gke_${MODULE_C_PROJECT}_${MODULE_C_LOCATION}_${MODULE_C_CLUSTER}"

gcloud container clusters get-credentials "$MODULE_C_CLUSTER" \
  --location "$MODULE_C_LOCATION" --project "$MODULE_C_PROJECT"
kubectl --context "$MODULE_C_CONTEXT" -n "$MODULE_C_NAMESPACE" \
  get deployment orders-service payments-service data-service module-c-auth-probe
python3 module-c/gcp/verify_live.py
```

Preparar las páginas de Monitoring → Dashboards, Monitoring → Alerting, Logs Explorer y Security Command Center del proyecto configurado. Los nombres de dashboards, políticas y canales asociados se recuperan en `module-c/gcp/.state/verification-latest.json`. Abrir el incidente observado durante la prueba cuando exista; no inventar un enlace ni presentar un incidente ajeno como evidencia.

En la misma terminal, mantener el túnel local al servicio independiente:

```bash
kubectl --context "$MODULE_C_CONTEXT" -n "$MODULE_C_NAMESPACE" \
  port-forward service/module-c-auth-probe 8080:8080
```

Usar otra terminal, situada también en la raíz del repositorio, para los generadores. El túnel sirve para autenticación; no demuestra tráfico N-S de VPC Flow Logs. No mostrar Secrets, tokens, claves ni información personal durante la grabación.

Si se necesita una instantánea CVE reciente, ejecutar antes de grabar, con Trivy instalado y acceso autorizado al registro:

```bash
python3 module-c/gcp/scan_live_images.py --project "$MODULE_C_PROJECT" --scan --publish
```

La descarga y el escaneo pueden tardar más que el video. Conservar su recibo real y los reportes. Si fallan, informar la falla; no sustituirla por una instantánea ficticia ni por cero vulnerabilidades.

## Guion sugerido

| Tiempo | Mostrar y ejecutar | Explicación sugerida |
|---|---|---|
| 0:00–0:40 | Alcance autorizado y deployments disponibles. | “La contribución corresponde a observabilidad de red y seguridad en GCP. La prueba de autenticación usa un servicio independiente y conserva las aplicaciones del equipo”. |
| 0:40–1:20 | Dashboard de seguridad y configuración de sus fuentes. | “Los paneles de red representan registros de flujo muestreados, no solicitudes, conexiones únicas ni bytes. N-S y E-W siguen los CIDR del plan”. |
| 1:20–2:30 | Prueba de autenticación, logs y política. | “Cada rechazo real debe devolver 401 y producir un evento con servicio, motivo y contexto de traza, sin registrar credenciales”. |
| 2:30–4:40 | Canary de red y su alcance. | “La prueba crea recursos temporales internos y genera tráfico limitado al puerto del plan. Esta regla identifica un comportamiento definido para el laboratorio, no cualquier amenaza”. |
| 4:40–5:40 | Inventario de imágenes, recibo CVE y frescura. | “Se muestran los conteos realmente obtenidos. Cada CVE se deduplica entre las imágenes y se asigna a su mayor severidad”. |
| 5:40–6:30 | Estado de SCC y configuración de notificaciones. | “Se distingue activación de SCC, asociación de un canal y recepción de una notificación: cada afirmación necesita su propia evidencia”. |
| 6:30–7:30 | Limpieza y limitaciones observadas. | “Se informa el tiempo medido hasta abrir el incidente, el estado del escaneo periódico y cualquier requisito pendiente”. |

## Autenticación: ejecutar y correlacionar

Con el port-forward activo:

```bash
python3 module-c/security/auth_probe.py --url http://127.0.0.1:8080 --count 8 --interval 1
```

Comprobar las respuestas 401 reales y conservar en privado el recibo con los identificadores de traza y las horas UTC. El generador también registra el inicio, la finalización y la duración de la prueba.

Para consultar exactamente las fuentes configuradas, ejecutar:

```bash
python3 module-c/gcp/deploy.py evidence --project "$MODULE_C_PROJECT"
python3 module-c/gcp/verify_live.py
```

Usar el filtro de autenticación del plan en Logs Explorer y seleccionar el intervalo de la ejecución. Confirmar `event=authentication_failed`, `service_name=module-c-auth-probe`, `reason` y el contexto de traza. Mostrar una coincidencia entre el identificador del recibo y el del log sin publicarlos en el repositorio.

Abrir la política **Modulo C - autenticaciones fallidas**. Si existe un incidente de esta prueba, registrar en privado su apertura y calcular la diferencia frente al primer evento fallido. El final del script no es la apertura del incidente. No afirmar detección en menos de dos minutos sin medirla. La asociación del canal no demuestra recepción de correo; para afirmar entrega, comprobar el mensaje recibido.

## Red: interpretar y limpiar la prueba

Mostrar primero el plan y luego ejecutar el generador acotado:

```bash
python3 module-c/gcp/network_canary.py
python3 module-c/gcp/network_canary.py --apply
```

El comando sin `--apply` informa namespace, puerto y volumen previstos. La ejecución imprime las horas reales del cliente y el volumen recibido. Conservar esos datos como evidencia privada. No confundir la creación del Job con el inicio efectivo del tráfico.

Después ejecutar nuevamente `deploy.py evidence` y `verify_live.py`. Usar el filtro de puerto inesperado del plan para revisar los registros de la fuente seleccionada. Confirmar protocolo, puerto de destino, origen, destino y namespace. No sumar registros duplicados de dos backends. Los tipos de recurso de Logging y Monitoring deben coincidir con los verificados en la instalación.

Los registros están sujetos a muestreo, agregación y demora de ingestión. Por eso el número de solicitudes enviadas no tiene que coincidir con el número de registros. Una regla de puerto inesperado tampoco equivale a un firewall DENY. Verificar por separado la aparición del incidente **Modulo C - puerto inesperado entre servicios** y su hora de apertura.

El canary intenta eliminar sus tres recursos al finalizar, también ante errores normales. Verificarlo usando las variables de configuración en una terminal donde se haya ejecutado la preparación:

```bash
kubectl --context "$MODULE_C_CONTEXT" -n "$MODULE_C_NAMESPACE" \
  get deployment,service,job -l app=module-c-network-canary
```

Si una interrupción dejó recursos, inspeccionar su propiedad y eliminar únicamente los del ensayo:

```bash
kubectl --context "$MODULE_C_CONTEXT" -n "$MODULE_C_NAMESPACE" \
  delete deployment,service,job module-c-network-canary --ignore-not-found
```

## CVEs, SCC y cierre honesto

Mostrar el `live-inventory.json`, `receipt.json` y `snapshot.json` del escaneo correspondiente. Esos archivos, junto con los reportes originales y sus hashes, se guardan en el directorio operacional de la ejecución y no se publican automáticamente. Leer los conteos desde el recibo real, sin números de ejemplo.

El helper escanea los digests inmutables observados de las aplicaciones declaradas. No incluye automáticamente sidecars, nodos ni complementos. Explicar que el conteo es de CVEs únicos y que una corrección disponible no significa que la imagen ya esté corregida.

La actualización proporcionada es manual. Revisar la hora del escaneo y la alerta por ausencia o antigüedad superior a una hora. Reenviar una instantánea antigua no vuelve reciente su escaneo. Una programación periódica solo puede presentarse como instalada después de desplegarla y verificarla.

En SCC mostrar el nivel y los hallazgos si está activado. Si existe un impedimento de organización o permisos, registrar su evidencia privada y explicar el requisito pendiente. Habilitar la API y publicar CVEs de Trivy no sustituyen la activación de SCC. No cambiar la organización ni contratar un nivel de pago para completar esta demostración.

Al terminar, detener el port-forward con `Ctrl+C` y conservar privadamente el video, capturas con fechas, recibos y horarios de incidentes. `verify_live.py` ayuda a recopilar evidencia; no certifica por sí solo activación de SCC, entrega de correo ni cumplimiento del tiempo objetivo de detección.
