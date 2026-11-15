# Plan técnico — Sistema de Alertas Climáticas

> Cómo vamos a cumplir `spec.md`. Cada decisión lleva su "por qué" y la alternativa descartada.

## 1. Stack

| Pieza | Elección | Por qué |
|---|---|---|
| API | FastAPI | Pedido del challenge. Async nativo, OpenAPI gratis. |
| ORM | SQLAlchemy 2.0 async + `asyncpg` | Driver async más rápido para Postgres; API tipada `Mapped[]`. |
| Migraciones | Alembic (env async) | Pedido. Se corren como paso separado (`migrate` en compose), nunca en el arranque de la API. |
| Scheduler | APScheduler 3.x `AsyncIOScheduler` | Ligero, corre en el mismo event loop que nuestro código async. Celery/RQ agregan broker (Redis/Rabbit) para un problema que Postgres ya resuelve. |
| Validación / config | Pydantic v2 + pydantic-settings | Estándar de FastAPI; settings 12-factor. |
| Logs | structlog (JSON) | Logs consultables por campo (`run_id`, `alert_id`). |
| Tests | pytest + pytest-asyncio + testcontainers[postgres] + httpx | Tests de integración contra **Postgres real**: usamos `ON CONFLICT`, `SKIP LOCKED`, índices parciales y advisory locks, que SQLite no tiene. |
| Calidad | ruff + mypy --strict + pre-commit, gestión con `uv` | |

## 2. Modelo de datos

Código: `src/agrobot/infrastructure/db/models.py`.

```
users 1───* fields 1───* weather_forecasts      (lo escribe el ingestor; mock)
  │            │
  │            └───* alerts 1───* alert_notifications   (outbox)
  └────────────────────┘ (user_id denormalizado en alerts)
```

### weather_forecasts
| Columna | Tipo | Nota |
|---|---|---|
| id | BIGSERIAL PK | Tabla de alto volumen (campos × días × eventos); bigint es más chico y rápido que UUID. |
| field_id | UUID FK → fields | |
| forecast_date | DATE | El dato de negocio es "el día". |
| event_type | ENUM weather_event | |
| probability | NUMERIC(5,2) CHECK 0..100 | Exacto: comparar `>=` con floats da sorpresas en los bordes (59.999999). |
| issued_at | TIMESTAMPTZ | Emisión del proveedor (corrida del modelo). Mide la frescura (RF-13b). |
| source, ingested_at | | Trazabilidad. |

- `UNIQUE (field_id, forecast_date, event_type)` → el ingestor hace **upsert condicional**
  (`… DO UPDATE … WHERE EXCLUDED.issued_at > weather_forecasts.issued_at`): reintentos son no-op y
  un lote viejo que llega tarde no pisa uno nuevo.
- Índice `(field_id, event_type, forecast_date)` para el join del evaluador con rango de fechas.

### alerts
| Columna | Tipo | Nota |
|---|---|---|
| id | UUID PK | ID público en la API; no expone volumen. |
| user_id | UUID FK | Denormalizado: autorización y listados sin join; base para multi-tenancy. |
| field_id | UUID FK | |
| event_type | ENUM | |
| threshold | NUMERIC(5,2) CHECK `> 0 AND <= 100` | Umbral 0 dispararía siempre. |
| status | ENUM (active, paused) | |
| deleted_at | TIMESTAMPTZ NULL | Borrado lógico: el historial de notificaciones mantiene su FK. |
| version | INT | Optimistic locking (`version_id_col`) → ETag / If-Match. |

- Índice único **parcial** `(field_id, event_type, threshold) WHERE deleted_at IS NULL` (RF-03), permite recrear una regla borrada.
- Índice parcial `(field_id, event_type) WHERE deleted_at IS NULL AND status='active'`: el evaluador sólo toca alertas vivas.

### alert_breach_states (máquina de estados por alerta + fecha)
| Columna | Nota |
|---|---|
| alert_id, forecast_date | PK. |
| is_breached | ¿El último pronóstico vigente estaba ≥ umbral? |
| episode | Cantidad de cruces hacia arriba. Se incrementa sólo en la transición armada → disparada. |
| last_probability, last_issued_at | Debug y auditoría. |

Es lo que permite "re-avisar siempre que baje y vuelva a subir" sin spamear mientras se mantiene arriba (RF-11).

### alert_notifications (outbox)
| Columna | Nota |
|---|---|
| alert_id, forecast_date, episode | `UNIQUE` → **una notificación por cruce** (RF-11). |
| event_type, probability, threshold, issued_at | **Snapshot** del momento del disparo: el historial no cambia si después editan el umbral o llega otro pronóstico. |
| status, attempts, next_attempt_at, last_error, sent_at | Máquina de estados de entrega + reintentos + lease. |

- Índice parcial `(next_attempt_at) WHERE status='pending'`: la cola del dispatcher queda chica aunque la tabla crezca.

### Convenciones
- Enums Postgres nativos (`values_callable` → se guarda `"rain"`, no `"RAIN"`).
- `naming_convention` en el `MetaData` → nombres de constraints deterministas para Alembic.
- Todo instante es `TIMESTAMPTZ` (UTC). El `Clock` inyectable devuelve `now()` en UTC y el dominio
  calcula "hoy" **por campo** con `zoneinfo` sobre `fields.timezone` (IANA). La imagen Docker
  incluye `tzdata`. Así el sistema corre igual en un server de Frankfurt o de Buenos Aires.

## 3. Arquitectura (Clean Architecture)

Regla de dependencia: **las flechas apuntan hacia adentro**.
`api` / `worker` / `infrastructure` → `application` → `domain`. El dominio no importa nada de SQLAlchemy ni FastAPI.

```
agrobot-alerts/
├── Specs/                      spec.md · plan.md · tasks.md
├── docs/openapi.yaml           contrato (fuente de verdad)
├── src/agrobot/
│   ├── config.py               Settings (pydantic-settings)
│   ├── domain/                 ── PURO: sin I/O, sin frameworks
│   │   ├── enums.py
│   │   ├── entities.py         dataclasses frozen: AlertRule, ForecastPoint, Breach
│   │   ├── evaluator.py        evaluate(...) → Decision  ← corazón testeable
│   │   └── errors.py           AlertNotFound, DuplicateAlert, VersionConflict…
│   ├── application/            ── casos de uso + puertos (Protocols)
│   │   ├── ports.py            AlertRepository, ForecastRepository, NotificationRepository,
│   │   │                       NotificationSender, Clock, UnitOfWork, JobLock
│   │   └── use_cases/
│   │       ├── manage_alerts.py         create / list / get / update / delete
│   │       ├── evaluate_alerts.py       EvaluateAlerts.run() → RunReport
│   │       └── dispatch_notifications.py
│   ├── infrastructure/         ── adaptadores
│   │   ├── db/ models.py · session.py · repositories.py · uow.py · locks.py
│   │   ├── notifications/logging_sender.py   mock de WhatsApp
│   │   └── clock.py
│   ├── api/                    ── presentación HTTP
│   │   ├── main.py             app factory + lifespan (engine dispose)
│   │   ├── deps.py             composition root: UoW, current_user
│   │   ├── schemas.py          Pydantic request/response (espejo de openapi.yaml)
│   │   ├── errors.py           domain errors → problem+json
│   │   └── routers/ alerts.py · notifications.py · forecasts.py · health.py
│   └── worker/                 ── proceso aparte
│       ├── main.py             `python -m agrobot.worker` + señales SIGTERM
│       └── scheduler.py        registro de jobs en APScheduler
├── migrations/                 Alembic (env.py async) · versions/
├── scripts/seed_forecasts.py   simula el job de ingesta (upsert)
├── tests/
│   ├── unit/                   evaluator + use cases con fakes en memoria
│   ├── integration/            repositorios, locks, migraciones (testcontainers)
│   └── api/                    httpx.AsyncClient contra la app
├── Dockerfile · docker-compose.yml (db, migrate, api, worker)
└── pyproject.toml · alembic.ini · README.md
```

**Por qué esta separación:** el evaluador (lo que más casos de borde tiene) se testea sin base
(RNF-05); los casos de uso se testean con fakes; sólo los adaptadores necesitan Postgres.
Cambiar WhatsApp por SMS o APScheduler por Celery toca un adaptador, no el dominio.

## 4. Estrategia del background job

### 4.1 Dónde corre
Un **proceso worker separado** (`python -m agrobot.worker`) con `AsyncIOScheduler`.

- **No** `BackgroundTasks`: está atado a un request; no es periódico.
- **No** scheduler dentro del `lifespan` de FastAPI: con 4 workers de uvicorn × 3 réplicas tendríamos
  12 schedulers evaluando lo mismo, y la API y el job no podrían escalar por separado.
- **No** `while True: await asyncio.sleep()`: sin manejo de misfires, solapamientos ni apagado ordenado.

### 4.2 Dos jobs, desacoplados por el outbox

```
[ingestor] ─upsert→ weather_forecasts
                          │
        (cada 15')  EvaluateAlerts ──INSERT … ON CONFLICT DO NOTHING──→ alert_notifications (pending)
                                                                              │
        (cada 30")  DispatchNotifications ──claim SKIP LOCKED──→ NotificationSender (mock WhatsApp)
                                                                              │
                                                               sent / retry con backoff / failed
```

Separar **detectar** de **entregar** es la decisión clave: si WhatsApp está caído, la evaluación
sigue; si el evaluador corre 10 veces, se entrega una vez.

### 4.3 EvaluateAlerts
1. `pg_try_advisory_xact_lock(<clave del job>)`. Si otra réplica lo tiene → loguea `skipped` y sale (RNF-03).
2. Toma `now_utc` del `Clock`.
3. Recorre las alertas evaluables **por lotes** (keyset pagination por `id`, `BATCH_SIZE=500`) para
   acotar memoria. Por cada lote trae en **una** query: los pronósticos de esos `(field_id, event_type)`
   (con la zona del campo) y los `alert_breach_states` existentes.
4. Llama a la función **pura** del dominio, que para cada (alerta, fecha) decide la transición:
   ```python
   def evaluate(
       rule: AlertRule,                  # threshold, field_tz, ...
       forecast: ForecastPoint,          # date, probability, issued_at
       prev: BreachState | None,
       *, now_utc: datetime, horizon_days: int, max_age: timedelta,
   ) -> Decision:                        # Skip(reason) | Hold | Rearm | Fire(episode)
   ```
   | Caso | Resultado |
   |---|---|
   | fecha < hoy del campo, o > hoy + horizonte | `Skip(out_of_window)` |
   | `now - issued_at > max_age` | `Skip(stale)` → no toca el estado |
   | prob ≥ umbral y (sin estado o armada) | `Fire(episode = prev.episode + 1)` |
   | prob ≥ umbral y ya disparada | `Hold` |
   | prob < umbral | `Rearm` (estado → armada; si ya estaba, no-op) |
5. Persiste en la **misma transacción**: upsert de `alert_breach_states` y
   `INSERT INTO alert_notifications … ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING`.
6. Commit por lote (una falla no pierde todo el trabajo previo; reintentar es seguro por la idempotencia).
7. Loguea un `RunReport` (evaluadas, disparos, re-armadas, stale, duplicadas, duración).

**Resolución temporal:** el evaluador sólo "ve" un descenso si corre entre el pronóstico que baja y el
que vuelve a subir. Como el upsert pisa el valor, el intervalo del evaluador (15') tiene que ser mucho
menor que el de la ingesta (horas). Si el worker estuvo caído y se perdió una bajada, no se re-avisa;
para garantizarlo al 100 % la extensión es evaluar reactivamente tras cada ingesta (`LISTEN/NOTIFY`).

**Flapping:** con "re-avisar siempre", un pronóstico que oscila 59 ↔ 61 avisa en cada oscilación.
Dejamos `REARM_MARGIN` configurable (default 0 = comportamiento pedido): con 5, se re-arma recién
cuando baja de umbral − 5.

*Alternativa a escala:* hacer todo el paso 3-5 en un único `INSERT … SELECT … JOIN` en SQL.
Es más rápido pero saca la regla de negocio del dominio. Para este volumen priorizamos testeabilidad;
la query queda documentada como optimización si el lote se vuelve el cuello de botella.

### 4.4 DispatchNotifications (patrón *lease*)
1. Tx corta: `SELECT … WHERE status='pending' AND next_attempt_at <= now() ORDER BY next_attempt_at
   LIMIT 50 FOR UPDATE SKIP LOCKED` → para esas filas `attempts += 1`,
   `next_attempt_at = now() + LEASE` → **commit**. (Las réplicas toman filas distintas, no se bloquean.)
2. Envía **fuera de la transacción** (no se mantiene una conexión abierta durante I/O de red), en
   paralelo acotado con `asyncio.Semaphore`.
3. Éxito → `sent`, `sent_at`. Error → `next_attempt_at = now() + backoff(attempts)` o `failed` si
   `attempts >= MAX_ATTEMPTS`, guardando `last_error`.
4. Si el worker muere a mitad de envío, el lease vence y otra réplica la retoma.
   La entrega es **at-least-once**; el `notification.id` viaja como clave de deduplicación al
   proveedor (WhatsApp/Twilio aceptan una) para acercarse a exactly-once efectivo.

### 4.5 Configuración del scheduler
```python
scheduler.add_job(
    evaluate,
    IntervalTrigger(minutes=settings.eval_interval_min, jitter=30),
    id="evaluate_alerts",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=60,
    replace_existing=True,
)
scheduler.add_job(
    dispatch,
    IntervalTrigger(seconds=settings.dispatch_interval_s),
    id="dispatch_notifications",
    max_instances=1,
    coalesce=True,
)
```
- `max_instances=1`: si una corrida tarda más que el intervalo, no se solapa consigo misma (en el proceso).
- `coalesce=True`: si se perdieron N disparos (worker dormido), corre una vez, no N.
- `jitter`: evita que todas las réplicas peguen a la base en el mismo segundo.
- Job store en memoria a propósito: los jobs se registran en código al arrancar; el estado
  importante vive en Postgres, no en el scheduler.
- SIGTERM → `scheduler.shutdown(wait=True)` + `engine.dispose()`: termina la corrida en curso.
- Cada corrida abre su propia sesión (`async_sessionmaker`); nunca se comparte una sesión entre jobs.

## 5. API — decisiones
- Contrato en `docs/openapi.yaml`; test que compara contra `app.openapi()` para que no se desincronicen.
- Recurso ajeno → 404 (no revela existencia). Errores → problem+json.
- `Idempotency-Key` opcional en POST (tabla `idempotency_keys`, TTL 24 h) — reintentos de red desde WhatsApp.
- PATCH con `If-Match` (ETag = `version`) → 412 si cambió.
- DELETE idempotente (204 siempre).
- Paginación por cursor (keyset `(created_at, id)`), no OFFSET: estable y O(1) por página.

## 6. Riesgos y extensiones
| Tema | Decisión hoy | Extensión |
|---|---|---|
| Re-aviso si baja y vuelve a subir | Sí, por episodio (máquina de estados) | `REARM_MARGIN` anti-flapping; aviso de "se levantó la alerta" en `Rearm`. |
| Bajada perdida con el worker caído | Se documenta | Evaluación reactiva post-ingesta (`LISTEN/NOTIFY`). |
| Ingesta caída (todo stale) | Warning + métrica por campo | `/health/ready` del worker degradado si no hay pronósticos vigentes en N horas. |
| Varias notificaciones al mismo usuario | Una por disparo | Agrupar en el dispatcher por `user_id` en un único mensaje. |
| Volumen alto | Lotes + índices parciales | `INSERT … SELECT` set-based; particionar `weather_forecasts` por mes. |
| Evaluación reactiva | Polling | Que el ingestor emita `NOTIFY forecasts_updated` y el worker evalúe sólo esos campos. |
| Historial de pronósticos | Se pisa (upsert) | Tabla de versiones si se necesita auditoría. |
| Limpieza | — | Job de retención de notificaciones viejas / pronósticos pasados. |

## 7. Decisiones tomadas durante la implementación

Durante la construcción de las fases 1 a 6 se tomaron las siguientes decisiones técnicas complementarias y conservadoras, alineadas con Spec-Driven Development:

1. **Idempotencia completa en `POST /alerts` (`Idempotency-Key`)**:
   - Se implementó la tabla `idempotency_keys` con clave única `(user_id, idempotency_key)`, hash criptográfico SHA-256 del cuerpo de la petición (`payload_hash`), status HTTP y cuerpo de respuesta serializado en JSON, con caducidad tras 24 horas (`expires_at`).
   - Si se reintenta con la misma clave y el mismo payload, se reenvía la respuesta cacheada sin re-ejecutar lógica de negocio.
   - Si se reintenta con la misma clave pero distinto payload, se interrumpe y rechaza con `422 Unprocessable Entity` (`IdempotencyConflict` mapeado a RFC 9457 `application/problem+json`).

2. **Serialización numérica de `Percentage` para conformidad estricta con OpenAPI**:
   - `docs/openapi.yaml` define `Percentage` como `type: number` (ej. 60.0 o 59.99).
   - En Python y SQLAlchemy se utiliza `Decimal` para evitar errores de precisión de punto flotante en comparaciones de umbrales (`probability >= threshold`).
   - Para evitar que Pydantic v2 serialice `Decimal` como `string` en JSON de salida, se definió un tipo anotado con serializador explícito: `PercentageNumber = Annotated[Decimal, PlainSerializer(lambda x: float(x), return_type=float, when_used="json")]`. Esto preserva la exactitud interna y satisface el contrato OpenAPI 3.0 al 100%.

3. **Aislamiento de sesiones en `DispatchNotifications` concurrente**:
   - El worker despacha notificaciones fuera de la transacción de lease usando un semáforo de concurrencia (`asyncio.Semaphore`).
   - Dado que `AsyncSession` de SQLAlchemy no admite operaciones concurrentes en la misma sesión, se inyectó una fábrica de unidades de trabajo (`uow_factory: Callable[[], UnitOfWork]`).
   - Cada tarea concurrente de envío adquiere una sesión/UoW dedicada e independiente para actualizar el estado final (`sent` o reintento con backoff/`failed`), evitando condiciones de carrera de event loop o sesión compartida.

4. **Tratamiento estricto de fechas y zonas horarias en UTC**:
   - Todo instante en base de datos es almacenado en `TIMESTAMPTZ` y normalizado en UTC a través del puerto `Clock`.
   - La fecha de corte local ("hoy") se evalúa por campo convirtiendo el instante UTC actual a la zona horaria IANA del campo con `zoneinfo.ZoneInfo(field.timezone)`.
   - La ventana de evaluación queda estrictamente definida como `[hoy_campo, hoy_campo + horizon_days]`.

5. **Aislamiento multi-inquilino y seguridad en endpoints REST**:
   - La autenticación simulada lee el encabezado `X-User-Id`.
   - Si un usuario intenta acceder, modificar o eliminar una alerta o recurso perteneciente a otro usuario o inexistente, el sistema responde invariablemente con `404 Not Found`, previniendo la enumeración de recursos o filtración de existencia de identificadores ajenos.

6. **Control de concurrencia optimista en modificaciones (ETag / If-Match)**:
   - Los endpoints de actualización exigen o validan el encabezado `If-Match: W/"<version>"` o `"<version>"`.
   - Si la versión provista en la cabecera no coincide exactamente con la versión persistida en la base de datos, se rechaza la operación con `412 Precondition Failed` (`VersionConflict`).

