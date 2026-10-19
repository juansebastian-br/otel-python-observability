# Módulo D: caos controlado en GCP

Este módulo ejecuta dos experimentos en el namespace exclusivo
`observability-chaos-d`: añadir 200 ms a `POST /payments` (service-b) e
inyectar HTTP 503 con probabilidad 10 % en `GET /data/health` de data-service.
Esta última ruta ejecuta una consulta PostgreSQL real; `/health` se conserva
para comprobar que el proceso está vivo y queda excluida del caos.

## Aislamiento

Los servicios originales de `observability-lab` se consultan para resolver sus
imágenes por digest y recuperar únicamente `main.py` y `otel.py`. No se leen
sus contraseñas. Se despliegan copias en el namespace de caos, con una base
PostgreSQL desechable y datos sintéticos. La copia de data-service usa solo GCP.
La base de este sandbox corre en GKE: no se presenta como Cloud SQL.

Una política de red permite comunicación dentro del sandbox y resolución DNS;
los servicios son internos. Ningún contenedor necesita NET_ADMIN, privilegios
de administrador o token de cuenta de servicio. La inyección ocurre en el
middleware ASGI, antes de la operación de negocio. El control no se expone por
HTTP: se escribe en un archivo del pod después de verificar su propiedad.
Un TTL absoluto de hasta 180 segundos limita cada activación y una guarda
monotónica impide extenderla por un cambio de reloj. Configuración inválida,
namespace incorrecto o TTL vencido significan caos apagado.

## Señales y medición

- **Métricas:** contador HTTP e histograma de duración de la ruta intervenida,
  incluido el retraso inyectado. Prometheus consulta cada 5 segundos.
- **Logs:** cada falla realmente aplicada genera `chaos.applied` con hora UTC,
  experimento y `trace_id`.
- **Trazas:** el contexto W3C se propaga y los spans se exportan por OTel al
  Collector y Jaeger del sandbox. Un encabezado de respuesta por sí solo no
  demuestra que la traza haya llegado al backend.
- **Alertas:** p99 superior a 200 ms en payments; tasa de 5xx superior a 0,5 %
  en data. Ventana 60 segundos, al menos 20 muestras y condición sostenida
  30 segundos. Son condiciones independientes para detectar ambos fallos.

Los SLO declarados para la prueba son disponibilidad 99,5 % y 99 % de las
solicitudes con duración de hasta 200 ms. Cada experimento contempla 15 minutos
de baseline, hasta 3 minutos de caos y 5 minutos de recuperación, con 5
solicitudes por segundo. Los tiempos de transición se registran por separado.
La prueba requiere baseline sano antes de activar la falla.

Se calcula el presupuesto de errores sobre **la ventana de laboratorio**:
`presupuesto = solicitudes × (1 − objetivo)`; `consumo = fallos / presupuesto`.
Para latencia se cuentan las solicitudes que exceden 200 ms. Estos resultados
no representan el presupuesto mensual de producción. Las llegadas omitidas
por saturación del generador se reportan y hacen inválida la evaluación del SLO.

El objetivo MTTD es **menos de 120 segundos** desde el primer `chaos.applied`
del servidor hasta la primera observación real de la alerta en estado firing.
También se conserva la hora de recepción del webhook como medida separada.
El campo `startsAt`/`activeAt` puede representar el inicio de la condición
pendiente y no se utiliza como evidencia de notificación. Si una alerta no se
observa, el resultado es no demostrado, nunca cero segundos.

## Componentes

| Directorio | Responsabilidad |
|---|---|
| `runtime/` | Inyección acotada, trazas y métricas de las rutas exactas |
| `gcp/` | Despliegue aislado y comprobaciones de propiedad |
| `monitoring/` | Prometheus, Alertmanager y receptor de evidencia |
| `experiment/` | Carga fija, ejecución por fases y análisis |

Las evidencias de ejecución, fuentes recuperadas y datos del proyecto se
guardan en `.state/`, excluido de Git. Los comandos reproducibles y resultados
verificados se documentan después de ejecutar el laboratorio; la presencia de
estas configuraciones no equivale a haber demostrado resiliencia.
