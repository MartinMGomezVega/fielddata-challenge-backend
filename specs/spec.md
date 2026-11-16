# Spec — Sistema de Alertas Climáticas (Agrobot)

> Qué tiene que hacer el sistema y cómo sabemos que lo hace. El *cómo* técnico vive en `plan.md`.

## 1. Problema

Agrobot ya ingesta pronósticos (por campo, por día futuro, por evento climático, con una
probabilidad). Los productores necesitan enterarse **a tiempo** cuando un evento que les
importa (helada, lluvia, granizo…) supera un nivel de riesgo que ellos mismos definen.

## 2. Glosario

| Término | Definición |
|---|---|
| Campo (`field`) | Lote de un usuario. Unidad geográfica del pronóstico. |
| Pronóstico (`forecast`) | `(field, forecast_date, event_type) → probability ∈ [0, 100]`. Lo escribe el ingestor (fuera de alcance, mockeado). |
| Alerta (`alert`) | Regla del usuario: "avisame si P(`event_type`) ≥ `threshold` en `field`". |
| Disparo (`breach`) | Un pronóstico que cumple una alerta activa para una fecha dentro de la ventana. |
| Notificación | Registro persistido de un disparo + su estado de entrega (outbox). |
| Hoy (de un campo) | Fecha local actual en la zona IANA del campo (`fields.timezone`). Independiente de dónde corra el servidor. |
| Ventana de evaluación | `[hoy del campo, hoy del campo + HORIZON_DAYS]`. |
| Episodio | Cada vez que el pronóstico de una alerta+fecha cruza el umbral **hacia arriba**. |
| Pronóstico vigente | Su `issued_at` (emisión del proveedor) tiene menos de `MAX_FORECAST_AGE_HOURS`. |

## 3. Requisitos funcionales

**Gestión de alertas (API REST, contrato en `docs/openapi.yaml`)**

- RF-01 Un usuario crea una alerta indicando campo, evento y umbral (0 < umbral ≤ 100).
- RF-02 Sólo puede crear alertas sobre **sus** campos; un campo ajeno se comporta como inexistente (404).
- RF-03 No pueden existir dos alertas vivas idénticas (mismo campo + evento + umbral) → 409.
  Sí se permiten varios umbrales para el mismo evento (ej. 50 = "atención", 80 = "crítico").
- RF-04 Lista sus alertas con filtros (campo, evento, estado) y paginación por página (`page`, `page_size`).
- RF-05 Modifica el umbral o pausa/reactiva una alerta. Campo y evento son inmutables.
- RF-06 Elimina una alerta (borrado lógico; el historial se conserva). Operación idempotente.
- RF-07 Consulta el historial de notificaciones de una alerta y su bandeja general.
- RF-08 Consulta el pronóstico almacenado de un campo (lectura, soporte).

**Evaluación (background)**

- RF-09 Periódicamente (default cada 15 min) se evalúan todas las alertas **activas y no borradas**
  contra los pronósticos de la ventana de evaluación.
- RF-10 Hay disparo si `probability >= threshold` (inclusive: "a partir de").
- RF-11 Se notifica en cada **cruce hacia arriba** del umbral (por alerta y fecha). Mientras el
  pronóstico siga por encima no se repite el aviso; si baja por debajo y vuelve a superarlo,
  **se re-avisa siempre**. Correr el job N veces con el mismo dato no genera avisos extra.
- RF-12 Si varias alertas del mismo campo/evento se superan (umbrales distintos), cada una notifica.
- RF-13 Pronósticos de fechas pasadas (respecto del "hoy" del campo) no se evalúan.
- RF-13b Un pronóstico no vigente (`issued_at` más viejo que `MAX_FORECAST_AGE_HOURS`, default 24)
  no dispara ni re-arma: el estado queda como estaba. Se registra un warning y una métrica de
  "datos desactualizados" por campo para que operaciones vea que la ingesta está fallando.
- RF-13c El ingestor sólo reemplaza un pronóstico por otro con `issued_at` más nuevo.
- RF-13d Si se edita el umbral, el próximo ciclo evalúa contra el nuevo valor: si el pronóstico
  actual ya lo supera y el estado estaba armado, se avisa.

**Entrega (mock)**

- RF-14 Las notificaciones se entregan por un puerto `NotificationSender`. La implementación del
  challenge loguea el mensaje (sin WhatsApp real).
- RF-15 Si la entrega falla se reintenta con backoff exponencial hasta `MAX_ATTEMPTS`; luego queda `failed`.

## 4. Requisitos no funcionales

- RNF-01 **Asincronía end-to-end**: FastAPI async + SQLAlchemy `AsyncSession` + `asyncpg`. Nada bloquea el event loop.
- RNF-02 **Idempotencia** garantizada por constraints de base, no por "chequear antes de insertar".
- RNF-03 **Concurrencia segura**: correr 2+ réplicas del worker no duplica evaluaciones ni envíos.
- RNF-04 El job de evaluación no corre dentro del proceso de la API (escalan por separado).
- RNF-05 Lógica de evaluación **pura** (sin I/O) y testeable en milisegundos.
- RNF-06 Esquema versionado con Alembic; la app nunca hace `create_all`.
- RNF-07 Observabilidad: logs estructurados (JSON) con métricas por corrida (alertas evaluadas,
  disparos, insertadas, duplicadas, enviadas, fallidas, duración). Health checks `live`/`ready`.
- RNF-08 Errores en `application/problem+json` (RFC 9457).
- RNF-09 Configuración 12-factor por variables de entorno.

## 5. Criterios de aceptación (Given / When / Then)

| ID | Escenario |
|---|---|
| AC-01 | **Given** alerta frost ≥ 60 y pronóstico frost 60.00 para mañana **When** corre el evaluador **Then** se crea 1 notificación pending (borde inclusivo). |
| AC-02 | Igual que AC-01 con 59.99 → 0 notificaciones. |
| AC-03 | El evaluador corre 2 veces seguidas sin cambios → sigue habiendo 1 notificación. |
| AC-04 | Pronóstico 90 → 30 → 90 para la misma fecha (cambio brusco), evaluando en cada paso → 2 notificaciones (episodios 1 y 2). |
| AC-04b | Pronóstico 70 → 85 → 95 (sigue arriba) → 1 notificación. |
| AC-04c | Pronóstico 90 → stale → 90 → 1 notificación (stale no cuenta como bajada). |
| AC-04d | Llega un lote con `issued_at` más viejo que el guardado → el pronóstico no cambia. |
| AC-04e | Campo en `Asia/Tokyo`, servidor en UTC, 20:00 UTC → "hoy" del campo es el día siguiente en UTC; la ventana se calcula con esa fecha. |
| AC-05 | Alertas rain ≥ 50 y rain ≥ 80, pronóstico 85 → 2 notificaciones (una por alerta). |
| AC-06 | Pronóstico ≥ umbral en 3 fechas distintas → 3 notificaciones. |
| AC-07 | Alerta pausada o borrada → 0 notificaciones. |
| AC-08 | Pronóstico de ayer o fuera del horizonte → ignorado. |
| AC-09 | Pronóstico con `issued_at` de hace más de 24 h → no dispara + warning + métrica. |
| AC-10 | Alerta de otro evento en el mismo campo → no dispara. |
| AC-11 | Dos workers corren el evaluador a la vez → mismo resultado que uno solo. |
| AC-12 | Sender falla 1 vez y luego funciona → notificación `sent`, `attempts = 2`. |
| AC-13 | Sender falla siempre → `failed` tras `MAX_ATTEMPTS`, `last_error` con la causa. |
| AC-14 | POST con campo ajeno → 404. Alerta duplicada → 409. Umbral 0 o 101 → 422. |
| AC-15 | PATCH con `If-Match` desactualizado → 412. |
| AC-16 | DELETE dos veces → 204 ambas; la alerta deja de evaluarse; su historial sigue consultable. |

## 6. Fuera de alcance

Integración real con WhatsApp, autenticación real (se simula con `X-User-Id`), el job de ingesta
real (se provee un seeder mock), agrupado de varias notificaciones en un solo mensaje, aviso de
"se levantó la alerta" cuando baja. Ver "Extensiones" en `plan.md`.

Quedan afuera a propósito: no los pide el challenge y agregan infraestructura o reglas de producto
que conviene validar con negocio antes de construir. El diseño actual los soporta sin rediseño
(detalle en `plan.md` §6).

| Tema | Por qué hoy no | Cómo se incorpora |
|---|---|---|
| Agrupar avisos por usuario en un único mensaje | Es una decisión de UX del canal (WhatsApp), no del motor de alertas. | En el dispatcher, agrupando la cola `pending` por `user_id`; el modelo no cambia. |
| Métricas Prometheus | Los `RunReport` estructurados ya exponen los contadores en logs. | Exportar los mismos contadores con `prometheus-client` desde api y worker. |
| Evaluación reactiva post-ingesta | El polling cada 15' es más simple y alcanza para la cadencia de los modelos (horas). | `LISTEN/NOTIFY` desde el ingestor; el caso de uso ya evalúa por lote de campos. |
| Aviso de "se levantó la alerta" | No lo pide el enunciado. | La transición `Rearm` ya existe en el dominio; sólo falta encolar el aviso. |
| Autenticación real | El challenge pide endpoints y job, no auth. | Reemplazar la dependencia `X-User-Id` por JWT; ningún router cambia. |
