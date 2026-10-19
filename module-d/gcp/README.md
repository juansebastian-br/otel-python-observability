# Despliegue aislado en GKE

Ejecutar desde la raíz del repositorio en Cloud Shell, con acceso al proyecto y contexto GKE ya seleccionados. El script verifica ambos y no cambia el contexto de forma implícita.

```sh
python module-d/gcp/deploy.py --project crafty-dynamics-239114 \
  --latency-threshold-ms 200 --out .state/module-d
```

Este primer paso consulta los recursos originales y lee exclusivamente `/app/app/main.py` y `/app/app/otel.py` de los contenedores pagos/data. Genera un manifiesto privado sin valores de secretos, un inventario de imágenes por digest, hashes de fuente original/adaptada y el contrato de monitoreo. No aplica recursos. El umbral de 200 ms debe quedar declarado antes de medir la línea base; cambiarlo requiere otro directorio y otra línea base.

Revisar `.state/module-d/manifest.json`, `inventory.json`, `source-hashes.json` y `monitoring/contract.json`. Después:

```sh
python module-d/gcp/deploy.py --project crafty-dynamics-239114 \
  --latency-threshold-ms 200 --out .state/module-d --apply
kubectl -n observability-chaos-d get deployments,pods,services
kubectl -n observability-chaos-d get events --sort-by=.lastTimestamp
```

La salida `APPLIED_READINESS_NOT_YET_VERIFIED` confirma la aplicación de manifiestos, no disponibilidad, ejecución del caos ni cumplimiento del MTTD. Verificar todas las réplicas disponibles, dos targets Prometheus `UP`, las reglas cargadas y peticiones reales exitosas a ambas rutas antes de iniciar la línea base. Si ocurre un error después del primer apply, el recibo conserva `APPLY_STARTED_NOT_COMPLETED`: pueden existir recursos parciales que deben inspeccionarse.

## Aislamiento y datos

- Namespace fijo `observability-chaos-d`; namespace existente y colisiones de recursos requieren ambas etiquetas de propiedad. Todos los recursos se validan antes de escribir. No hay escrituras en `observability-lab` ni cambios a C/B.
- Imágenes de negocio tomadas de pods listos con propiedad Deployment → ReplicaSet → Pod y digest único; se excluye únicamente `istio-proxy`. El overlay reutiliza sus dependencias y código real. La adaptación de data-service a una conexión GCP exige cuatro patrones exactos; cualquier diferencia detiene el despliegue.
- PostgreSQL 16 Alpine efímero, base `module_d` inicializada con `db-init.sql`. No se conecta a Cloud SQL/RDS ni se copian credenciales originales. La nueva contraseña aleatoria va a Kubernetes exclusivamente por stdin. Reaplicar reutiliza el Secret existente sin leer su contenido ni rotarlo.
- UID no root, `fsGroup`, sin token de cuenta de servicio, sin capacidades añadidas, sin volúmenes host ni exposición pública. Istio queda deshabilitado solo en las copias.
- NetworkPolicy deniega ingreso/egreso por defecto y permite comunicación dentro del sandbox. DNS permite únicamente TCP/UDP53 a kube-dns y las dos IP locales de DNS documentadas por GKE. No se permite HTTP al servidor de metadata ni salida hacia recursos compartidos.
- Aplicaciones: `module-d-payments:8001`, `module-d-data:8002`; control local `/var/run/chaos/control.json`, sin endpoint administrativo. La ausencia del archivo mantiene el caos desactivado.
- Collector contrib0.161.0 recibe los tres pilares; exporta trazas a `module-d-jaeger:4317` y logs/métricas al exportador debug básico. Las alertas rápidas usan métricas HTTP del wrapper y Prometheus, no se afirma una integración de Cloud Monitoring para estas reglas.
- Jaeger2.21.0: consulta interna `module-d-jaeger:16686`, memoria1Gi y máximo20.000 trazas. Collector512Mi. PostgreSQL, Prometheus, Alertmanager, receptor y Jaeger tienen almacenamiento efímero: guardar evidencia antes de reiniciar/eliminar.
- Fuente/configuración se identifica mediante checksum en cada Pod para reiniciar procesos afectados al reaplicar cambios. Cambios al esquema reinician la base efímera y exigen una nueva línea base.

`originals-before.json` y `originals-after.json` contienen identidades y hashes de definiciones de Deployments, Services, ConfigMaps y NetworkPolicies originales; nunca Secrets. Una diferencia hace fallar el proceso para revisión. Los manifiestos y fuentes deben permanecer privados en `.state/`; un `.gitignore` interno evita añadirlos al repositorio accidentalmente.

## Fuentes primarias

- [Seguridad de GKE Autopilot](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-security).
- [DNS local y NetworkPolicy en GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nodelocal-dns-cache).
- [Descubrimiento DNS y direcciones del resolver](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/service-discovery).
- [Configuración Jaeger2.21.0](https://github.com/jaegertracing/jaeger/blob/v2.21.0/cmd/jaeger/config.yaml).
- [Collector0.161.0](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.161.0).
- [Imagen oficial PostgreSQL16 Alpine y UID70](https://github.com/docker-library/postgres/blob/master/16/alpine3.23/Dockerfile).
