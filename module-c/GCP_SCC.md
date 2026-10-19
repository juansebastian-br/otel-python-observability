# Security Command Center: estado y cierre del requisito

**Proyecto: `crafty-dynamics-239114`. Verificación: 18 de octubre de 2026.**

La consulta real `gcloud projects describe crafty-dynamics-239114 --format=json` devolvió el proyecto sin el campo `parent`. El proyecto fue creado en 2019. Este resultado indica que no pertenece a una organización, según el criterio de [Resource Manager para proyectos sin organización](https://docs.cloud.google.com/resource-manager/docs/handle-special-cases). Por tanto, la activación de Security Command Center (SCC) permanece **pendiente por un requisito del proyecto**, no por falta de código del módulo.

La consola confirmó el mismo impedimento en la vista `security/command-center/no-org`: exige pertenecer a una organización. La captura real se incluye en el informe del módulo, con el estado observado y la ruta para completar el requisito.

Google exige que el proyecto pertenezca a una organización para activar SCC, incluso con alcance de proyecto y nivel Standard. Habilitar `securitycenter.googleapis.com` por sí solo no activa SCC. [Requisitos oficiales de activación](https://docs.cloud.google.com/security-command-center/docs/activate-scc-for-a-project).

## Comprobar el estado sin modificar recursos

Ejecutar en Cloud Shell:

```bash
gcloud projects describe crafty-dynamics-239114 --format=json
gcloud projects get-ancestors crafty-dynamics-239114 --format=json
gcloud organizations list --format=json
```

Guardar las respuestas completas con fecha UTC junto a la evidencia del laboratorio. Una lista vacía de organizaciones accesibles no prueba por sí sola que el proyecto carezca de organización; usar `describe` y la jerarquía de ancestros. Consultar también [SCC para este proyecto](https://console.cloud.google.com/security/command-center?project=crafty-dynamics-239114) y guardar su estado real, sin presentar la pantalla de bienvenida como una activación completada.

## Ruta para completar SCC dentro de GCP

1. **Preferida: sandbox institucional.** Obtener un proyecto de laboratorio dentro de una organización de la universidad o una organización administrada por el equipo. El administrador debe dar acceso al proyecto y habilitar SCC Standard en ese ámbito. Si cambia el proyecto, ejecutar los scripts del módulo con su ID real y recoger evidencia de ese destino.
2. **Conservar este proyecto mediante migración.** El propietario puede asociarlo a una organización que administre y que haya sido autorizada como destino. Para el caso sin organización, Google documenta `roles/resourcemanager.projectIamAdmin` en el proyecto y `roles/resourcemanager.projectCreator` en la organización de destino. Revisar antes los permisos y políticas heredados. Volver a “Sin organización” requiere intervención de Cloud Customer Care. Esta entrega no realiza una migración. [Procedimiento y requisitos](https://docs.cloud.google.com/resource-manager/docs/handle-special-cases).
3. **Organización propia.** Puede obtenerse mediante Cloud Identity o Google Workspace asociado a un dominio administrado por el propietario. Las organizaciones independientes que no requieren dominio se asignan a nuevos clientes de prueba; Google indica que no están disponibles para cuentas existentes. No se puede asumir que una cuenta existente reciba una automáticamente. [Organizaciones de Cloud Identity](https://docs.cloud.google.com/resource-manager/docs/manage-google-cloud-resources) y [organizaciones independientes](https://docs.cloud.google.com/resource-manager/docs/standalone-organization-overview).

Una vez resuelto el requisito de organización, abrir SCC en el proyecto correcto, seleccionar **Get Standard / Obtener Standard** y completar **Activate / Activar**. La guía actual pide los roles `roles/iam.securityAdmin` y `roles/securitycenter.admin` en el proyecto. Standard activa Security Health Analytics; la exploración inicial puede tardar hasta 24 horas. No es necesario seleccionar la prueba de Premium para esta actividad. [Activación de Standard por proyecto](https://docs.cloud.google.com/security-command-center/docs/activate-scc-for-a-project).

## Evidencia necesaria para marcar este punto como completado

| Evidencia | Qué demuestra |
|---|---|
| Proyecto y ancestros con organización identificada | Se cumple el requisito de jerarquía. |
| SCC → Settings / Configuración → Tier Details | Nivel y ámbito efectivamente activos. |
| Servicios de SCC con Security Health Analytics habilitado | El detector está configurado. |
| Hallazgos, filtros aplicados y fecha de consulta | Resultado real del análisis; una lista vacía debe conservarse como tal. |
| Hallazgo real con recurso y recomendación, cuando exista | Permite explicar una respuesta basada en datos sin fabricar incidentes. |

El dashboard de autenticaciones, tráfico y CVEs puede funcionar mientras se resuelve SCC. El inventario Trivy de imágenes constituye evidencia de CVEs del alcance declarado; **no sustituye la activación de SCC**. La integración de vulnerabilidades de Artifact Registry con SCC exige activación a nivel de organización y Container Scanning habilitado: no debe prometerse solo por activar Standard en un proyecto. [Requisitos de integración](https://docs.cloud.google.com/security-command-center/docs/vulnerability-assessment-ar-overview).

**Estado de este requisito al emitir esta guía: pendiente.** Solo cambiarlo a completado cuando existan las evidencias anteriores en un proyecto con organización.
