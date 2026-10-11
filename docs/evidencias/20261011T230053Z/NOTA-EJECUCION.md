# Nota de ejecución y alcance

Fecha: 11 de octubre de 2026. Entorno: Docker Desktop local en Windows/WSL2.

Se completó una pareja baseline/OTel, cada escenario con 50 usuarios concurrentes durante 300 segundos y 30 segundos de calentamiento. Ambos procesos k6 terminaron con código cero, cero errores HTTP y todos los checks aprobados. Hay 55 muestras válidas de recursos por escenario.

El controlador inicial terminó con código 1 DESPUÉS de completar y guardar ambas mediciones: la verificación final intentó conectar con la aplicación antes de que terminara su reinicio. Se agregó una espera de disponibilidad de orders/payments antes de esa verificación en benchmark/run.py. No cambió la carga ni la instrumentación medidas. Después se ejecutaron explícitamente verify_correlation.py y verify_grafana_api.py sobre la instrumentación restaurada. Ambos resultados PASS se conservan en correlation-final/. No se atribuye código cero al controlador original completo.

La correlación final contiene 17 spans y tres logs de negocio con un trace_id común; se comprobaron ambos servicios, spans SQL y relaciones padre/hijo, además del aprovisionamiento de enlaces en Grafana y la salud de Loki vía API.

La interfaz del navegador mantuvo una restricción incluso después de la autorización del usuario. No se incluyeron capturas nuevas de Grafana o Jaeger ni se afirma haber comprobado visualmente el clic del enlace. Las figuras nuevas del informe se elaboraron a partir de los datos exportados. Las capturas cloud del documento original pertenecen al trabajo previo del equipo.

Las mediciones son exploratorias: una sola pareja sin intervalo de confianza y en un equipo compartido con el generador. La memoria media de las aplicaciones disminuyó; esto no demuestra que OTel reduzca memoria de forma general. La memoria total del laboratorio aumentó y comprende otro límite de medición: ocho contenedores, incluidos backends con retención. No son resultados de GKE/ECS.

Se incluyen los originales k6, recursos, trazas/logs exportados, comparación, versiones e imágenes. docker-info.json permanece en la corrida local y se excluye de este snapshot por contener información adicional del host que no se requiere en la entrega. SHA256SUMS.txt permite verificar los archivos del snapshot; source-sha256.json identifica los archivos relevantes del código final. El único ajuste del controlador posterior a la medición es la espera descrita arriba.

Los hashes de source-sha256.json corresponden a los bytes del código en el equipo de ejecución; la normalización de finales de línea de Git puede cambiar esos hashes en otro sistema. Los archivos de evidencia se conservan sin conversión de finales de línea mediante .gitattributes, para mantener verificable SHA256SUMS.txt después de clonar.

La publicación del aporte puede verificarse en el historial Git del repositorio. Publicar estos archivos no implica haber desplegado o medido un entorno nuevo en GKE/ECS.
