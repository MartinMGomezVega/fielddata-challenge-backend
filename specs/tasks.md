# Tasks

Orden pensado para que cada fase deje algo verde y commiteable. `[x]` = hecho en esta iteración.

## Fase 1 — Esqueleto
- [x] `pyproject.toml` (uv), ruff, mypy strict, pytest-asyncio, pre-commit
- [x] `config.py` (Settings: DATABASE_URL, TZ, HORIZON_DAYS, intervals, BATCH_SIZE, MAX_ATTEMPTS, STALE_FORECAST_HOURS)
- [x] `Dockerfile` (multi-stage, usuario no-root) + `docker-compose.yml` (db, migrate, api, worker)
- [x] Logging estructurado (structlog) compartido por api y worker

## Fase 2 — Persistencia
- [x] Alembic con `env.py` async; migración `0001_initial` (enums, tablas, índices parciales)
- [x] Test de migraciones: `upgrade head` → `downgrade base` → `upgrade head`
- [x] `session.py` (engine + `async_sessionmaker`, `expire_on_commit=False`)
- [x] `UnitOfWork` + repositorios (Alert, Forecast, Notification)
- [x] `locks.py` (advisory lock por job)
- [x] `scripts/seed_forecasts.py` — mock del ingestor con upsert (incluye casos borde: 59.99 / 60.00, fechas pasadas, stale)

## Fase 3 — Dominio (TDD primero)
- [x] Tests de `evaluate()` cubriendo AC-01…AC-10 y AC-04b…e, incluido `REARM_MARGIN`
- [x] Property-based (hypothesis): nunca dispara si prob < umbral; mismo dato dos veces no re-dispara; `episode` monótono
- [x] `entities.py` + `evaluator.py` (máquina de estados: Skip / Hold / Rearm / Fire)

## Fase 4 — Casos de uso
- [x] `manage_alerts` con fakes en memoria (ownership, duplicado, versión)
- [x] `evaluate_alerts` (lotes, ON CONFLICT, RunReport)
- [x] `dispatch_notifications` (lease, backoff, failed) + `LoggingSender`
- [x] Tests de integración: AC-03, AC-11 (dos evaluadores concurrentes), AC-12, AC-13 contra Postgres real

## Fase 5 — API
- [x] Schemas Pydantic + routers según `openapi.yaml`
- [x] Handlers problem+json, `X-User-Id`, ETag/If-Match, pagination by page/page_size
- [x] `Idempotency-Key` en POST /alerts (tabla `idempotency_keys`, TTL 24 h; misma key + body distinto → 422)
- [x] Health `live` / `ready`
- [x] Tests API: AC-14, AC-15, AC-16 + test de contrato (openapi generado vs `docs/openapi.yaml`)

## Fase 6 — Worker
- [x] `worker/scheduler.py` + `worker/main.py` con shutdown ordenado
- [x] Test: el job registrado tiene `max_instances=1`, `coalesce=True`

## Fase 7 — Entrega
- [x] README: cómo correr (compose / local), cómo testear, decisiones, qué faltó
- [x] CI (GitHub Actions): lint, mypy, tests con servicio postgres
