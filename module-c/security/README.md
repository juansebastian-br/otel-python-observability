# Telemetría de seguridad del módulo C — GCP

## Autenticación verificable

`services/orders/app/security.py` comprueba una clave mediante `hmac.compare_digest`. La integración en el `POST /orders` del repositorio está desactivada por defecto. Activarla exige `SECURITY_AUTH_ENABLED=true`, `DEPLOYMENT_ENVIRONMENT=sandbox` y una `SECURITY_API_KEY` de al menos 24 bytes, suministrada mediante un Secret de Kubernetes. También admite los entornos `local` y `test`; rechaza `production`. `/health` permanece disponible para los probes.

El despliegue de demostración independiente usa `probe_app.py` y `Dockerfile`, con contexto de construcción en la raíz del repositorio. Escucha en el puerto 8080 y usa el mismo control de autenticación. Permite demostrar fallos reales sin reemplazar los servicios desplegados por otros integrantes. Su `POST /orders` comprueba la credencial y devuelve `authenticated`; no crea órdenes ni accede a una base de datos. El alcance de esta evidencia es `module-c-auth-probe`, no la seguridad de autenticación de los otros servicios.

Cada rechazo emite exactamente un objeto JSON a stdout:

- `event=authentication_failed`, `severity=WARNING`.
- `service_name`, `reason=missing_credential|invalid_credential`.
- `trace_id` y `span_id` tomados del contexto OTel real; son nulos cuando no existe un contexto válido.
- `timestamp` UTC. No incluye claves, cabeceras, usuarios, IP ni cuerpos de solicitudes.

La métrica de logs debe seleccionar únicamente stdout de los contenedores del namespace del laboratorio. Los eventos de rechazo no se emiten adicionalmente al exportador OTLP de logs, lo que evita contarlos dos veces. Las trazas del servicio independiente se exportan a `OTEL_EXPORTER_OTLP_ENDPOINT`; el valor por defecto es `otel-collector:4317`. La configuración del despliegue debe indicar el nombre real del Collector.

El generador `auth_probe.py` envía ocho credenciales deliberadamente inválidas por defecto. Solo admite loopback o el hostname autorizado explícitamente mediante `--allow-host`; rechaza redirecciones, URLs con credenciales, rutas distintas de `/orders`, más de 20 solicitudes y frecuencias superiores a dos solicitudes por segundo. Se detiene si la respuesta no es 401. El cuerpo vacío evita que una configuración sin autenticación pueda crear una orden en el servicio del repositorio.

Con un port-forward al servicio de demostración en el puerto local 8080:

```powershell
python module-c/security/auth_probe.py --url http://127.0.0.1:8080 --count 8 --interval 1
```

Los identificadores de trazas impresos por el generador permiten localizar las solicitudes. Para demostrar éxito, utilizar la clave del Secret en una solicitud autorizada, sin imprimirla ni guardarla en los archivos de evidencia.

## CVEs activos: estado, no contador de hallazgos

`cve_snapshot.py scan` ejecuta Trivy para cada imagen declarada, con todas las severidades, vulnerabilidades sin corrección disponible incluidas, sin archivo local de exclusiones y con `--exit-code 0`. Ese parámetro permite que encontrar CVEs sea un resultado exitoso del escáner; los errores de ejecución siguen produciendo una salida distinta de cero y bloquean la publicación.

Una instantánea completa contiene los reportes originales, sus hashes SHA-256, el alcance, la versión de Trivy, las horas de inicio y finalización y los conteos recalculables. El conteo es de **identificadores CVE únicos en el conjunto de imágenes declarado**. Si un CVE aparece en varias imágenes o paquetes, se cuenta una vez, en la mayor severidad observada. Una corrección disponible no significa que la imagen instalada esté corregida. Los identificadores de avisos que no empiezan por CVE se informan por separado.

Solo una ejecución completa y verificable publica las cinco severidades, incluidos los ceros. Si falla cualquier imagen, falta un reporte, el reporte es parcial, los hashes no coinciden o los conteos se alteraron, no se publica una instantánea exitosa. Un fallo del escáner nunca significa cero CVEs.

Los argumentos de `scan` son `--image` repetible, `--output-dir` para un directorio nuevo y, opcionalmente, `--project crafty-dynamics-239114` para publicar tras el escaneo. Usar los **digests inmutables de las imágenes realmente desplegadas**. El listado de imágenes define el alcance de la evidencia: escanear las aplicaciones no equivale a escanear los nodos, el sistema operativo del clúster o todos los complementos.

La operación `publish` exige `--snapshot` y `--project`. Verifica nuevamente los hashes y conteos y rechaza una instantánea de más de 24 horas o con fecha futura. El punto de frescura conserva la hora original del escaneo, incluso si la instantánea se vuelve a publicar. El dashboard debe mostrar la edad y considerar obsoletos los datos de más de una hora; repetir la publicación no los vuelve recientes.

| Métrica | Tipo y recurso | Etiquetas | Significado |
|---|---|---|---|
| `custom.googleapis.com/security/active_cves` | `GAUGE`, `INT64`, `global` | `severity` | CVEs únicos activos por severidad en el alcance declarado |
| `custom.googleapis.com/security/cve_snapshot_timestamp` | `GAUGE`, `INT64`, `global` | Ninguna | Segundos Unix de finalización del último escaneo completo publicado |

Las severidades son `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` y `UNKNOWN`. El recurso `global` lleva `project_id`. El proceso usa la autenticación activa de `gcloud`, solicita un token en memoria y envía las series a Cloud Monitoring sin imprimir el token. El principal requiere permiso `monitoring.timeSeries.create`, normalmente mediante `roles/monitoring.metricWriter`.

Estas métricas complementan Security Command Center; no sustituyen su activación ni convierten los resultados de Trivy en hallazgos de SCC.

## Despliegue independiente en el sandbox GCP

Desde la raíz del repositorio en Cloud Shell, construir y publicar la imagen del servicio independiente:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
docker build -f module-c/security/Dockerfile -t us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final/module-c-auth-probe:v1 .
docker push us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final/module-c-auth-probe:v1
python3 module-c/security/deploy_probe.py \
  --project crafty-dynamics-239114 \
  --cluster otel-lab --location us-central1 \
  --namespace observability-lab \
  --registry us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final \
  --image us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final/module-c-auth-probe:v1 \
  --otlp-endpoint otel-collector:4317
```

Si Cloud Shell no puede subir la imagen directamente a Artifact Registry, construirla y publicarla mediante Cloud Build:

```bash
gcloud builds submit . \
  --project crafty-dynamics-239114 --region us-central1 \
  --config module-c/security/cloudbuild.yaml \
  --ignore-file module-c/security/cloudbuild.gcloudignore \
  --substitutions _IMAGE=us-central1-docker.pkg.dev/crafty-dynamics-239114/observability-final/module-c-auth-probe:v1
```

La lista permitida de `module-c/security/cloudbuild.gcloudignore` sube únicamente los cinco archivos necesarios para esta construcción y el propio archivo de exclusión. No incluye `.git`, `.state`, credenciales, pruebas, cachés ni evidencias. La exclusión se aplica solo al comando que la solicita y no cambia las construcciones de otros integrantes. `_IMAGE` es obligatorio y no tiene un destino implícito. Cloud Build construye y publica la imagen; después se ejecuta `deploy_probe.py` con los mismos argumentos de arriba. La identidad de ejecución de Cloud Build necesita escritura en el repositorio de Artifact Registry y permisos para escribir sus logs.

El helper obtiene las credenciales del clúster indicado y usa su contexto explícito. Antes de escribir comprueba que no existan objetos con el mismo nombre pertenecientes a otro administrador. Crea una clave aleatoria sin mostrarla ni guardarla en disco; si ya existe el Secret administrado, conserva su clave. Usa `API_KEY` como clave del Secret y la expone exclusivamente como `SECURITY_API_KEY` al contenedor. El nombre de Secret, Deployment y Service es `module-c-auth-probe`.

El pod corre sin privilegios, como usuario 65534, con sistema de archivos de solo lectura, capacidades eliminadas y sin token de ServiceAccount. Solicita 100m de CPU y 128Mi de memoria; sus límites son 500m y 256Mi. La inyección automática de Istio está desactivada para esta demostración. El Service es interno de tipo ClusterIP y no crea una dirección pública.

Para probarlo, mantener este comando en una terminal:

```bash
kubectl --context gke_crafty-dynamics-239114_us-central1_otel-lab -n observability-lab port-forward service/module-c-auth-probe 8080:8080
```

En otra terminal:

```bash
python3 module-c/security/auth_probe.py --url http://127.0.0.1:8080 --count 8 --interval 1
```

El servicio permanece disponible para la demostración. Cuando termine el laboratorio y ya se hayan conservado las evidencias, eliminar únicamente estos recursos con el contexto explícito:

```bash
kubectl --context gke_crafty-dynamics-239114_us-central1_otel-lab -n observability-lab delete deployment,service,secret module-c-auth-probe
```

La eliminación anterior también elimina la clave de demostración. No elimina ni modifica `orders-service`, `payments-service`, `data-service` ni el Collector.

## Verificación local

Instalar las dependencias de `services/orders/requirements.txt` en un entorno aislado y ejecutar, desde la raíz del repositorio:

```powershell
python -m unittest discover -s tests/module_c -v
```

Las pruebas usan las rutas ASGI reales, exportadores OTel en memoria y sustitutos de la base de datos y del servicio de pagos. Verifican autenticación, correlación de trazas, compatibilidad por defecto, límites del generador, deduplicación de CVEs, completitud, integridad de reportes y frescura. Estas pruebas son validación local y no prueban por sí solas ingestión de logs, entrega de alertas ni activación de SCC en GCP.

Referencias: [Trivy: formato de reportes](https://trivy.dev/docs/latest/configuration/reporting/), [GCP: métricas personalizadas](https://docs.cloud.google.com/monitoring/custom-metrics/creating-metrics).
