# Agrobot — Sistema de Alertas Climáticas

[![CI](https://github.com/MartinMGomezVega/fielddata-challenge-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/MartinMGomezVega/fielddata-challenge-backend/actions/workflows/ci.yml)
[![Contrato OpenAPI](https://img.shields.io/badge/OpenAPI-Swagger-85EA2D?logo=swagger&logoColor=black)](https://martinmgomezvega.github.io/fielddata-challenge-backend/)

Backend del challenge técnico de **FieldData**. Los productores configuran alertas sobre sus campos (*"avisame si la
probabilidad de helada supera el 60 %"*) y un worker en segundo plano evalúa periódicamente los pronósticos
guardados contra esas reglas y genera notificaciones (el envío por WhatsApp está simulado).

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL 16 · Alembic · APScheduler · Docker.
**Enfoque:** Spec-Driven Development (contrato y especificación primero, en [`specs/`](specs/)) y Clean Architecture.

---

## Cómo correrlo

Requiere Docker con Compose 2.24 o superior.

```bash
docker compose up --build -d
```

Un solo comando levanta todo, en este orden:

| Servicio | Qué hace |
|---|---|
| `db` | PostgreSQL 16 |
| `migrate` | Aplica las migraciones de Alembic y termina |
| `seed` | Carga datos de demo con pronósticos relativos a la fecha actual (sólo para demo; en producción la tabla la llena el job de ingesta) |
| `api` | API en http://localhost:8000 |
| `worker` | Evalúa las alertas al arrancar y cada 15 minutos; despacha notificaciones cada 30 segundos |

En menos de un minuto ya hay notificaciones generadas y enviadas. Para verificar:

```bash
curl http://localhost:8000/health/ready     # {"status":"ready"}
```

Las variables de entorno tienen valores por defecto; para cambiarlas, copiá `.env.example` a `.env`.

---

## Documentación de la API

| Vista | Dónde |
|---|---|
| **Contrato OpenAPI** (fuente de verdad) | [GitHub Pages](https://martinmgomezvega.github.io/fielddata-challenge-backend/) · local con `make docs` → http://localhost:8080 |
| **Swagger UI de la API en vivo** | http://localhost:8000/docs (ReDoc en `/redoc`) |

El contrato está en [`docs/openapi.yaml`](docs/openapi.yaml), y un test automatizado verifica que la API implementada coincida con él.

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/v1/alerts` | Crear alerta (acepta `Idempotency-Key`) |
| `GET` | `/api/v1/alerts` | Listar mis alertas: filtros por campo, evento y estado; paginación por cursor |
| `GET` · `PATCH` · `DELETE` | `/api/v1/alerts/{id}` | Detalle · cambiar umbral o pausar (`If-Match`) · borrado lógico |
| `GET` | `/api/v1/alerts/{id}/notifications` | Historial de avisos de una alerta |
| `GET` | `/api/v1/notifications` | Bandeja de notificaciones del usuario |
| `GET` | `/api/v1/fields/{id}/forecasts` | Pronóstico almacenado de un campo |
| `GET` | `/health/live` · `/health/ready` | Liveness y readiness |

La identidad se simula con el header `X-User-Id` (la autenticación real quedó fuera de alcance). Los errores
siguen RFC 9457 (`application/problem+json`).

---

## Probarlo

### Colección de Postman
Importá [`postman/agrobot.postman_collection.json`](postman/agrobot.postman_collection.json) y el environment
[`postman/agrobot.local.postman_environment.json`](postman/agrobot.local.postman_environment.json), y corré la colección.
También se puede correr desde la terminal con `make postman` (requiere Node). Son 37 requests con tests automáticos:

- **Flujo completo de una alerta:** crear, editar, pausar, filtrar, paginar y borrar.
- **Idempotencia y concurrencia:** `Idempotency-Key` y `If-Match`.
- **Validaciones:** 404, 409, 412 y 422.
- **Resultado del worker:** las notificaciones que generó a partir del seed.

La colección deja la base como estaba, así que se puede correr varias veces.

### Datos de demo

| Recurso | ID | Detalle |
|---|---|---|
| Usuario A | `11111111-1111-1111-1111-111111111111` | Juan Perez |
| Usuario B | `22222222-2222-2222-2222-222222222222` | Akira Tanaka |
| Campo Córdoba | `33333333-3333-3333-3333-333333333333` | de A, zona `America/Argentina/Cordoba` |
| Campo Tokio | `44444444-4444-4444-4444-444444444444` | de B, zona `Asia/Tokyo` |
| Alertas de A | `5555…` helada ≥ 60 · `6666…` lluvia ≥ 50 · `7777…` lluvia ≥ 80 | sobre el campo de Córdoba |

Los pronósticos incluyen casos de borde a propósito:

| Pronóstico | Resultado esperado |
|---|---|
| Helada 60.00 % | Dispara la alerta de helada (el borde es inclusivo) |
| Helada 59.99 % | No dispara |
| Lluvia 85 % | Dispara las alertas de lluvia ≥ 50 y ≥ 80 (dos umbrales sobre el mismo evento) |
| Pronóstico de ayer | Se ignora: fuera de la ventana |
| Pronóstico emitido hace 26 h | Se ignora: dato desactualizado |

Para ver lo que generó el worker:

```bash
curl -H "X-User-Id: 11111111-1111-1111-1111-111111111111" http://localhost:8000/api/v1/notifications
```

---

## Cómo funciona

### Flujo de punta a punta
```mermaid
sequenceDiagram
    autonumber
    actor P as Productor
    participant API as API (FastAPI)
    participant UC as ManageAlerts
    participant ING as Ingestor (mock)
    participant DB as PostgreSQL
    participant EV as Worker: EvaluateAlerts
    participant DOM as evaluate() (dominio puro)
    participant DSP as Worker: DispatchNotifications
    participant WA as NotificationSender (mock WhatsApp)

    Note over P,DB: 1. Configuración de la alerta
    P->>API: POST /api/v1/alerts (Idempotency-Key)
    API->>UC: create_alert(dto)
    UC->>DB: INSERT alerts (status=active, version=1)
    API-->>P: 201 Created + ETag W/"1"

    Note over ING,DB: 2. Ingesta de pronósticos
    ING->>DB: UPSERT weather_forecasts<br/>WHERE EXCLUDED.issued_at > issued_at

    Note over EV,DOM: 3. Evaluación (cada 15 min)
    EV->>DB: pg_try_advisory_lock
    alt lock ocupado por otra réplica
        EV-->>EV: skipped
    else lock adquirido
        loop por lote de 500 alertas activas
            EV->>DB: SELECT alertas + pronósticos + alert_breach_states
            EV->>DOM: evaluate(regla, pronóstico, estado previo, now_utc)
            alt Fire (cruzó el umbral hacia arriba)
                DOM-->>EV: Fire(episode = n + 1)
                EV->>DB: breach_state: armada → disparada (episode n+1)
                EV->>DB: INSERT alert_notifications (pending)<br/>ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING
            else Hold (sigue arriba)
                DOM-->>EV: Hold
                Note right of EV: sin cambios, no se re-avisa
            else Rearm (bajó del umbral)
                DOM-->>EV: Rearm
                EV->>DB: breach_state: disparada → armada
            else Skip (stale / fuera de ventana)
                DOM-->>EV: Skip(reason)
                Note right of EV: estado intacto + warning
            end
            EV->>DB: COMMIT del lote
        end
        EV->>DB: pg_advisory_unlock
    end

    Note over DSP,WA: 4. Despacho (cada 30 s)
    DSP->>DB: SELECT pending FOR UPDATE SKIP LOCKED<br/>attempts += 1, lease → COMMIT
    DSP->>WA: send(notification_id, teléfono, mensaje)
    alt envío OK
        DSP->>DB: pending → sent (sent_at)
    else error y attempts < MAX_ATTEMPTS
        DSP->>DB: pending (next_attempt_at = backoff exponencial)
    else error y attempts >= MAX_ATTEMPTS
        DSP->>DB: pending → failed (last_error)
    end

    Note over P,API: 5. Consulta
    P->>API: GET /api/v1/notifications
    API-->>P: 200 (episode, probability, threshold, status)
```

### Estado de una alerta por fecha pronosticada
```mermaid
stateDiagram-v2
    direction LR
    [*] --> Armada
    Armada --> Disparada: Fire · prob ≥ umbral
    Disparada --> Armada: Rearm · prob < umbral − margen
    note right of Disparada
        Fire suma 1 al episode y encola un aviso.
        Hold (sigue arriba) no cambia nada.
    end note
    note left of Armada
        Stale o fuera de ventana:
        el estado no cambia.
    end note
```

Se avisa cada vez que el pronóstico **cruza** el umbral hacia arriba. Mientras se mantiene arriba no se repite el aviso;
si baja y vuelve a subir, es un nuevo episodio y se avisa de nuevo. Un pronóstico desactualizado no cuenta como bajada.

### Estado de una notificación (outbox)
```mermaid
stateDiagram-v2
    direction LR
    state "pending en backoff" as backoff
    [*] --> pending: Fire
    pending --> sent: envío OK
    pending --> backoff: error
    backoff --> pending: vence next_attempt_at
    pending --> failed: attempts ≥ MAX_ATTEMPTS
    sent --> [*]
    failed --> [*]
```

---

## Decisiones técnicas

El detalle de cada decisión, con su alternativa descartada, está en [`specs/plan.md`](specs/plan.md).

| Tema | Decisión | Por qué |
|---|---|---|
| Idempotencia | Constraints de la base: `UNIQUE (alerta, fecha, episodio)` + `INSERT … ON CONFLICT DO NOTHING` | Correr el job N veces, o en varias réplicas, da el mismo resultado. Nada de *check-then-insert* |
| Detectar vs. entregar | Outbox: el evaluador escribe notificaciones `pending` y el dispatcher las envía | Si el proveedor de mensajería falla, la evaluación sigue; se reintenta sin duplicar |
| Background job | Proceso `worker` separado con APScheduler sobre asyncio | La API y el worker escalan por separado; sin un broker adicional (Postgres ya provee la cola y el lock) |
| Varias réplicas | Evaluador con `pg_try_advisory_lock`; dispatcher con `FOR UPDATE SKIP LOCKED` + lease | Una réplica evalúa; todas despachan en paralelo sin tomar la misma fila |
| Reintentos | Backoff exponencial (30 s, 60 s, …), `failed` tras 3 intentos | Tolera caídas transitorias del proveedor |
| Ingesta | Upsert condicional: sólo pisa si `issued_at` es más reciente | Un lote viejo que llega tarde no retrocede el pronóstico |
| Fecha "hoy" | Por campo, según su zona horaria IANA | El sistema se comporta igual sin importar dónde corra el servidor |
| Frescura | Se ignoran pronósticos emitidos hace más de 24 h | No alertar ni re-armar con datos que no son confiables |
| Precisión | `NUMERIC(5,2)` en vez de float | El borde del umbral (60.00 vs 59.99) se compara de forma exacta |
| Borrado | Lógico (`deleted_at`) | El historial de notificaciones se conserva |
| Concurrencia en la API | ETag / `If-Match` → 412 | Dos ediciones simultáneas no se pisan |
| Apagado del worker | Pausa el scheduler y espera los jobs en curso antes de cerrar | Un deploy no corta una evaluación a la mitad |

---

## Arquitectura

```
src/agrobot/
├── domain/          entidades, enums, errores y evaluate(): lógica pura, sin I/O ni frameworks
├── application/     casos de uso y puertos (Protocols): repositorios, UnitOfWork, Clock, Sender, JobLock
├── infrastructure/  SQLAlchemy (modelos, repositorios, mappers), advisory lock, reloj, sender simulado
├── api/             FastAPI: routers, schemas, manejo de errores, inyección de dependencias
└── worker/          scheduler de los jobs y apagado ordenado
```

Las dependencias apuntan hacia adentro: `domain` y `application` no importan SQLAlchemy ni FastAPI. Un test
(`tests/unit/test_architecture.py`) falla si alguien rompe esa regla.

---

## Tests y calidad

```bash
make test     # tests + cobertura (mínimo 90 % en dominio y aplicación)
make lint     # ruff + mypy --strict
```

- **Unitarios:** máquina de estados `evaluate()` con todos los casos de borde, tests de propiedades con Hypothesis y casos de uso con fakes en memoria.
- **Integración contra PostgreSQL real:** idempotencia, dos evaluadores en paralelo, reintentos y backoff, migraciones (sube, baja y vuelve a subir).
- **API:** códigos de error, idempotencia, ETag, y verificación del contrato contra `docs/openapi.yaml`.
- **CI (GitHub Actions):** lint, tipos, tests con Postgres y `docker build`.

Los tests necesitan un Postgres local con la base `agrobot_test` (usuario y contraseña `postgres`), o la variable `DATABASE_URL`.

---

## Desarrollo local sin Docker

Requiere Python 3.12, [uv](https://docs.astral.sh/uv/) y PostgreSQL 16 corriendo en `localhost:5432`.

```bash
uv sync --extra dev && source .venv/bin/activate
createdb agrobot && createdb agrobot_test
export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot"
alembic upgrade head
python scripts/seed_forecasts.py
uvicorn agrobot.api.main:app --reload     # API
python -m agrobot.worker.main             # worker, en otra terminal
```

Comandos útiles: `make up` · `make down` · `make migrate` · `make seed` · `make test` · `make lint` · `make format` · `make postman` · `make docs`.

---

## Fuera de alcance

No lo pedía el challenge. El diseño lo soporta sin cambios de fondo:

| Tema | Cómo se incorpora |
|---|---|
| Envío real por WhatsApp | Una nueva implementación de `NotificationSender` (WhatsApp Cloud API o Twilio), con plantillas aprobadas y webhooks de estado de entrega |
| Autenticación real | Reemplazar la dependencia `X-User-Id` por validación de JWT; ningún router ni caso de uso cambia |
| Evaluación reactiva | El ingestor emite `NOTIFY` al cargar pronósticos y el worker evalúa sólo los campos afectados; el polling queda como respaldo |
| Agrupar avisos por usuario | En el dispatcher, agrupando las notificaciones pendientes por `user_id` |
| Métricas y retención | Exportar a Prometheus los contadores que hoy van a los logs, y un job de limpieza de datos históricos |
