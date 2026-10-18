# Módulo C en GCP: red, seguridad y Golden Signals

Esta carpeta configura el proyecto `crafty-dynamics-239114` y descubre el clúster y la subred existentes. El alcance es GCP. No crea infraestructura AWS ni crea un nuevo clúster. Las pruebas locales comprueban la configuración; la ejecución y las evidencias reales se validan con los pasos siguientes.

Inventario confirmado el 18 de octubre de 2026: clúster Autopilot `otel-lab`, ubicación `us-central1`, VPC/subred `default` y namespace **`observability-lab`**. El namespace es distinto del utilizado por los manifiestos históricos del repositorio. No volver a desplegar esos manifiestos sobre las aplicaciones actuales. El instalador usa `observability-lab` por defecto y conserva las aplicaciones existentes. El proyecto no tiene organización y SCC muestra `no-org`: la activación SCC está bloqueada hasta resolver ese requisito.

## Qué instala

| Componente | Resultado |
|---|---|
| VPC Flow Logs | Habilitación en la subred descubierta si estaban desactivados; se conserva una configuración preexistente. |
| Métricas de logs | Autenticaciones fallidas, registros E-W, registros N-S entrantes/salientes, tráfico entre pods y puerto inesperado. |
| Alertas | Más de 4 autenticaciones fallidas/minuto, tráfico TCP al puerto 4444 entre pods del laboratorio, más de 120 registros/minuto entre pods durante un minuto, snapshot CVE ausente o con más de una hora. |
| Cloud Monitoring | Dashboard **Modulo C - Golden Signals de Seguridad**, ocho paneles. |
| Inventario CVE | Descriptores de métricas GAUGE; el escáner y publicador están en `module-c/security/`. |
| SCC | API habilitada y procedimiento explícito para activar/verificar **Standard**, sujeto a organización y permisos. Habilitar una API no activa SCC. |

La alerta de puerto inesperado es una regla de laboratorio explícita, no un detector universal de amenazas. La alerta de volumen usa un umbral ajustable: medir 15 minutos de carga normal antes de justificarlo en el informe. No se presenta como AIOps ni como baseline estadístico.

## 1. Abrir Cloud Shell y obtener la entrega

Abrir [Cloud Shell del proyecto](https://console.cloud.google.com/home/dashboard?project=crafty-dynamics-239114&cloudshell=true). Se necesitan Python 3, `gcloud` y `kubectl`, incluidos en Cloud Shell. Trabajar en una copia de esta rama:

```bash
git clone --branch feat/modulo-c-gcp-security https://github.com/juansebastian-br/otel-python-observability.git otel-modulo-c
cd otel-modulo-c
gcloud auth list
gcloud projects describe crafty-dynamics-239114 --format='yaml(projectId,projectNumber,parent)'
python3 -m unittest discover -s module-c/gcp -p 'test_*.py' -v
python3 module-c/gcp/deploy.py plan
```

El último comando es de solo lectura. Con un único clúster, descubre su nombre y ubicación; con varios clústeres se detiene y muestra las opciones. En ese caso usar `--cluster` y `--location` con los valores reales mostrados. El archivo generado `module-c/gcp/.state/plan.json` contiene todas las métricas, políticas y el dashboard, además del inventario descubierto. No contiene tokens. Se excluye de Git porque contiene información operacional.

Permisos para leer: inventario de GKE/Compute, proyecto, logs y Monitoring. Para aplicar: `compute.subnetworks.update`, gestión de métricas de Logging, descriptores/métricas/políticas/dashboards de Monitoring y `serviceusage.services.enable`. Asignar permisos en el proyecto del laboratorio; no es necesario conceder `Owner` ni publicar credenciales. Los permisos SCC dependen de la activación y su administrador.

## 2. Revisar el alcance y aplicar

Revisar `subnet`, `network`, `network_cidrs`, `subnet_before.logConfig`, `parent` y la visibilidad intranodo del plan. La subred debe estar destinada al sandbox. El script rechaza Shared VPC alojada en otro proyecto; una subred compartida dentro del mismo proyecto requiere revisión humana del inventario antes de seguir.

En **Monitoring → Alerting → Edit notification channels**, crear o seleccionar un canal y completar su verificación. Listar canales existentes:

```bash
gcloud beta monitoring channels list --project=crafty-dynamics-239114 --format='table(name,displayName,type,verificationStatus)'
```

Aplicar utilizando los valores descubiertos, sin sustituir nombres de infraestructura inventados:

```bash
read -r -p 'Nombre exacto de la subred sandbox mostrado en el plan: ' SANDBOX_SUBNET
read -r -p 'Recurso completo del canal de notificación existente: ' NOTIFICATION_CHANNEL
python3 module-c/gcp/deploy.py apply \
  --sandbox-subnet "$SANDBOX_SUBNET" \
  --notification-channel "$NOTIFICATION_CHANNEL"
```

Si fue necesario seleccionar clúster/ubicación al planificar, pasar las mismas opciones en `apply` y `evidence`. Los canales tienen formato `projects/.../notificationChannels/...`. Sin canal, las políticas todavía abren incidentes en Monitoring, pero **la entrega de notificaciones queda pendiente de demostrar**. El instalador no crea suscripciones de pago SCC ni altera reglas de firewall o conectividad. Los logs y métricas pueden generar consumo facturable.

La nueva configuración usa agregación de 5 segundos, metadatos completos y muestreo secundario 1.0. Esto **no elimina el muestreo primario**. Si Flow Logs ya estaban activos, conserva exactamente su configuración y avisa cuando filtros/metadatos puedan limitar la detección. La copia original queda en `.state/subnet-before.json` y no se sobreescribe en reejecuciones.

## 3. Security Command Center Standard

Consultar [Security Command Center del proyecto](https://console.cloud.google.com/security/command-center/overview?project=crafty-dynamics-239114). Si el proyecto ya hereda SCC desde una organización, verificar el nivel en **Settings → Tier details**. Si aparece la bienvenida y el proyecto cumple los requisitos, elegir **Get Standard → Activate**. No elegir una prueba Premium.

**Condición necesaria:** la documentación vigente exige que el proyecto esté asociado a una organización para la activación por proyecto. Si `gcloud projects describe` no muestra `parent`, no registrar SCC como activado ni presentar el dashboard propio como reemplazo de SCC. El administrador debe asociar un proyecto sandbox a una organización o proporcionar otro proyecto elegible. Si `parent` es un folder, comprobar su ascendencia organizacional con el administrador.

La aceptación exige capturar el nivel Standard y la vista Findings de SCC. Una vista vacía tras la primera evaluación puede ser válida, pero debe conservarse el horario y estado del escaneo; no equivale automáticamente a ausencia de riesgos. El inventario Trivy del módulo mide CVEs de las imágenes declaradas y sigue siendo independiente de SCC.

## 4. Generar y validar señales

### Red: prueba interna limitada

```bash
python3 module-c/gcp/network_canary.py
python3 module-c/gcp/network_canary.py --apply
python3 module-c/gcp/deploy.py evidence
```

La prueba usa la imagen ya desplegada de `orders-service`; crea un Deployment, un Service interno y un Job en el namespace del laboratorio. Envía 240 peticiones HTTP al puerto TCP 4444 durante aproximadamente dos minutos. Cada respuesta contiene exactamente 64 KiB de datos sintéticos, para un total acotado de **15 MiB de payload**; cabeceras y transporte añaden un volumen pequeño. El cuerpo permite mejorar la posibilidad de captura del muestreo primario respecto de respuestas diminutas. El cliente imprime eventos JSON UTC justo antes de la primera petición y al terminar, con la cantidad recibida; esos eventos delimitan la prueba real, no el tiempo de creación del Job. El proceso elimina sus tres recursos al terminar, también ante errores normales. No crea un LoadBalancer público ni modifica los servicios de negocio. Un cierre forzado de Cloud Shell puede interrumpir la limpieza: verificar y eliminar únicamente `deployment/module-c-network-canary`, `service/module-c-network-canary` y `job/module-c-network-canary` del namespace descubierto.

El puerto del test y de la política proviene del mismo plan. La regla selecciona **puerto de destino**, por lo que los paquetes de respuesta dirigidos a puertos efímeros no activan por sí solos esa regla. Si se cambia `--unexpected-port`, repetir plan/apply antes del test.

En Logs Explorer verificar registros `compute.googleapis.com/vpc_flows`, `reporter="SRC"`, protocolo TCP, destino 4444 y ambos namespaces. Después verificar el incidente **Modulo C - puerto inesperado entre servicios** y la entrega del canal. Los logs tienen muestreo y latencia de ingestión: el script no afirma MTTD real ni garantiza el objetivo de dos minutos del módulo D.

Para observar salida N-S normal sin enviar datos del laboratorio, realizar tres consultas HTTPS públicas desde el contenedor existente (solo biblioteca estándar de Python):

```bash
kubectl -n observability-lab exec deployment/orders-service -- python -c 'import urllib.request,time; [(urllib.request.urlopen("https://cloud.google.com/",timeout=20).read(1024),time.sleep(1)) for _ in range(3)]'
```

La consulta puede no dejar un registro por muestreo. Verificar evidencia antes de declarar el panel N-S validado. `kubectl port-forward` sirve para probar HTTP/autenticación, pero no se usa como evidencia de tránsito N-S de la VPC.

### Autenticación

Aplicar la integración de autenticación y el generador de pruebas de `module-c/security/`. El servicio publica JSON por stdout con `event="authentication_failed"`, `service_name`, `trace_id` y `reason`. La métrica lee solo stdout para evitar contar una copia exportada nuevamente por OTel. No extrae usuarios, claves, tokens ni trace IDs como etiquetas de métricas; el `trace_id` permanece en los logs para investigar.

Generar al menos cinco rechazos reales en una ventana de 60 segundos. Verificar primero los HTTP 401/403 reales, luego el JSON en Cloud Logging y finalmente el incidente. Una prueba de endpoint protegido no demuestra una protección completa de todas las rutas de negocio.

### CVEs activos y frescura

Ejecutar el escaneo/publicación de `module-c/security/` sobre **todas las imágenes incluidas en el inventario declarado**. Publicar únicamente cuando el lote completo termina correctamente. El contrato de métricas es:

El helper `scan_live_images.py` descubre el alcance concreto: todos los contenedores de aplicación declarados en `orders-service`, `payments-service` y `data-service`. Excluye sidecars, nodos y complementos del clúster. Comprueba los propietarios Deployment → ReplicaSet → Pod, un rollout estable con todas las réplicas listas y `imageID` reales de registro con digest `@sha256`. No escanea etiquetas mutables ni adivina el digest desde un hash local de containerd. Si falta una aplicación, hay una réplica no lista o aparecen digests distintos, se detiene.

Con Trivy instalado y el helper de credenciales de Artifact Registry configurado:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
python3 module-c/gcp/scan_live_images.py
python3 module-c/gcp/scan_live_images.py --scan --publish
```

Si Trivy está fuera de `PATH`, añadir `--scanner` con la ruta del ejecutable instalado. El primer comando Python solo inventaría; el segundo realiza el escaneo completo y publica. Antes de publicar vuelve a verificar que el inventario desplegado no haya cambiado durante el escaneo. Conserva inventario, reportes completos, hashes, snapshot y recibo en `.state/cve-scan-UTC/`. Las tres aplicaciones pueden compartir un digest, que se escanea una sola vez. El tiempo depende del tamaño de las imágenes y de la descarga de la base de vulnerabilidades.

| Métrica | Tipo | Etiquetas | Valor |
|---|---|---|---|
| `custom.googleapis.com/security/active_cves` | GAUGE / INT64 / unidad 1 | `severity` | Inventario actual: CRITICAL, HIGH, MEDIUM, LOW, UNKNOWN; publicar también ceros. |
| `custom.googleapis.com/security/cve_snapshot_timestamp` | GAUGE / INT64 / unidad s | Ninguna | Unix timestamp real de finalización del escaneo completo. |

Ambas usan recurso Monitoring `global` con `project_id`. La publicación repetida no debe cambiar la fecha real del escaneo. El dashboard calcula su edad y la alerta detecta valor antiguo o falta de publicaciones. Un escaneo fallido conserva el último inventario y produce un problema de frescura; **no publica cero CVEs**. El conteo es inventario, no número de hallazgos recibidos en logs. Programar el escaneo cada 15 minutos o ajustar el límite de frescura a la cadencia acordada.

## 5. Qué significan E-W y N-S

- **E-W:** ambos IP pertenecen a los CIDR primarios o secundarios de la VPC descubierta; se cuenta solo el reporter SRC.
- **N-S saliente:** origen dentro y destino fuera de esos CIDR; se cuenta SRC.
- **N-S entrante:** origen fuera y destino dentro; se cuenta DEST.
- Los gráficos muestran **registros de flujo muestreados por minuto**. No son número exacto de conexiones, requests, paquetes ni bytes.
- El alcance es la subred descubierta. Las redes conectadas por peering/on-prem no incluidas se clasifican fuera de esta frontera; explicitarlo en la sustentación. Si cambian los rangos, volver a planificar/aplicar.
- Los gráficos de red generales no requieren metadata de pods porque SNAT puede retirarla. La regla entre servicios sí exige ambos namespaces y cluster; validar que los metadatos estén presentes.
- Autopilot habilita visibilidad intranodo de forma predeterminada. En Standard puede faltar; el script avisa y no reinicia un clúster. Sin esa visibilidad, un intercambio entre pods del mismo nodo puede no registrarse.
- Flow Logs no se interpreta como un registro universal de decisiones de firewall. La regla de puerto inesperado refleja tráfico observado, no una acción `DENY`. Para una evidencia específica de denegación usar Firewall Rules Logging y sus campos reales, o verificar las anotaciones de paquetes descartados disponibles en la configuración concreta. Este módulo no modifica reglas para fabricar denegaciones.

## 6. Evidencia y aceptación

Capturar configuración y señales reales de la última hora sin modificar el plan ni sus canales aprobados:

```bash
python3 module-c/gcp/verify_live.py
```

Guarda la configuración actual de la subred, las seis métricas y hasta 100 logs por señal, series de Monitoring, políticas, asociación y estado de canales, CVEs y dashboard. `summary.json` distingue **configurado** de **observado**, indica resultados truncados o errores parciales y deja MTTD/entrega de notificaciones sin verificar; no inventa esos resultados. El resumen más reciente queda en `.state/verification-latest.json`. Los archivos completos se conservan en `.state/live-verification-UTC/`.

Conservar `.state/plan.json`, `.state/applied.json`, `subnet-before.json` y los JSON de `evidence` en un archivo privado de la entrega. Revisar antes de publicar: los IP/IDs/logs son información operacional. Un JSON vacío significa **sin registros observados**, no una comprobación aprobada.

| Criterio | Evidencia necesaria |
|---|---|
| Flow Logs | `logConfig.enable=true` y registros reales de la subred después de aplicar. |
| Tráfico anómalo | IP/puerto/namespace/horario en registros y el incidente correspondiente. |
| Respuesta accionable | Canal recibido, investigación en logs y eliminación del generador. |
| Autenticación | Rechazos reales, JSON con trace_id y gráfico/alerta. |
| N-S / E-W | Registros con ambos extremos que justifiquen la clasificación y paneles con datos. No generar tráfico hacia sistemas ajenos para completar un gráfico. |
| CVEs | Inventario completo, salida del escáner, cinco severidades y edad del snapshot. |
| SCC | Nivel Standard confirmado y Findings; si falta organización, documentar bloqueo. |

## Fuentes oficiales verificadas

- [Formato y metadatos de VPC Flow Logs](https://docs.cloud.google.com/vpc/docs/about-flow-logs-records).
- [Configurar Flow Logs](https://docs.cloud.google.com/vpc/docs/using-flow-logs).
- [Actualizar configuración de subred](https://docs.cloud.google.com/sdk/gcloud/reference/compute/networks/subnets/update).
- [Visibilidad intranodo GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/intranode-visibility).
- [Activación SCC por proyecto y requisitos](https://docs.cloud.google.com/security-command-center/docs/activate-scc-for-a-project).
- [Contadores basados en logs](https://docs.cloud.google.com/logging/docs/logs-based-metrics/counter-metrics).
- [Dashboards API v1](https://docs.cloud.google.com/monitoring/dashboards/api-dashboard).
- [PromQL en Cloud Monitoring](https://docs.cloud.google.com/monitoring/promql).
- [Alertas PromQL](https://docs.cloud.google.com/monitoring/promql/create-promql-alerts).
