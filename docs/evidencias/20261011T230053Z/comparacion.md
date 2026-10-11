# Resultados locales de overhead

Carga: 50 VUs, 300 s por escenario.

| Medida | Sin OTel | Con OTel | Diferencia |
|---|---:|---:|---:|
| p99 (ms) | 164.655 | 255.307 | 90.652 |
| CPU media (% de un nucleo) | 44.455 | 61.862 | 17.407 |
| Memoria media (MiB) | 323.142 | 223.363 | -99.779 |
| Memoria total laboratorio (MiB) | 653.634 | 876.630 | 222.996 |
| Throughput (req/s) | 47.660 | 47.312 | -0.348 |
| Fraccion de errores | 0.000 | 0.000 | 0.000 |

CPU overhead relativo: 39.155%.
p99 overhead relativo: 55.056%.
Validez de carga/muestreo: PASS.

CPU y memoria de aplicaciones suman orders + payments. Collector se informa por separado en el CSV. La memoria total suma los ocho contenedores, incluido PostgreSQL, y depende de la retencion acumulada de los backends. Docker memory usa memoria del contenedor excluyendo cache segun Docker; no es RSS del proceso.
Las muestras de recursos incluyen el drenaje final de hasta 15 s. El p99 corresponde a las peticiones completadas por k6.
Una sola pareja es exploratoria. Para mayor confianza repetir con --repetitions 3; el orden se alterna. La mediana de p99 por corrida no es un p99 combinado.
El generador, los servicios y los backends comparten equipo. No extrapolar estos resultados a GKE/ECS.
