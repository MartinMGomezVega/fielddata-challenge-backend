# Agrobot — Sistema de Alertas Climáticas

Backend para el monitoreo y notificación automática de eventos climáticos adversos en lotes agrícolas (Challenge Técnico Backend de FieldData).

Diseñado bajo **Spec-Driven Development (SDD)** y **Clean Architecture**, con persistencia en **PostgreSQL 16**, framework **FastAPI**, proceso en segundo plano con **APScheduler 3.10**, y tipado estricto con **Mypy** y **Pydantic v2**.

---

## 1. Qué es

Agrobot evalúa periódicamente pronósticos meteorológicos de 7 días contra reglas configuradas por productores agrícolas para sus campos (por ejemplo: *"avisarme si la probabilidad de lluvia supera el 60%"*). Cuando un pronóstico cruza el umbral hacia arriba, genera una notificación y la despacha al productor vía WhatsApp (simulado con Outbox Pattern).

### Características Principales
- **Máquina de estados por episodio**: Dispara una única notificación cuando la probabilidad cruza hacia arriba el umbral (`probability >= threshold`). Si la probabilidad se mantiene alta, no repite el aviso en ciclos posteriores. Si baja y vuelve a subir, detecta un nuevo episodio y re-avisa.
- **Ventana temporal y zonas horarias reales**: El corte de "hoy" se calcula para cada campo según su zona horaria IANA (`zoneinfo`). Un campo en Tokio y otro en Córdoba operan con sus ventanas locales de 7 días correctas en cualquier instante UTC.
- **Control de frescura (*Stale Forecasts*)**: Si la última emisión del pronóstico (`issued_at`) tiene más de 24 horas de antigüedad, se omite (`Skip(stale)`). Ni dispara ni rearma, registrando una advertencia estructurada.
- **Ingesta idempotente**: Upsert condicional (`ON CONFLICT ... DO UPDATE WHERE EXCLUDED.issued_at > weather_forecasts.issued_at`) garantizando que lotes demorados o reintentos no pisen datos recientes.
- **Despacho confiable (Transactional Outbox + Lease)**: Cola de notificaciones en base de datos con `FOR UPDATE SKIP LOCKED`, reintentos con backoff exponencial y jitter, y aislamiento concurrente.
- **API REST con contrato OpenAPI estricto**: Endpoints documentados en [docs/openapi.yaml](docs/openapi.yaml), con formato de error RFC 9457 (`application/problem+json`), paginación por cursor y soporte de `Idempotency-Key` en creación de alertas.

---

## 2. Arquitectura del Sistema

El proyecto sigue **Clean Architecture**, asegurando que el dominio sea puro e independiente de frameworks de presentación y de persistencia:

### Flujo de punta a punta
```mermaid
sequenceDiagram
    autonumber
    participant Ingest as Ingesta Mock / Cron
    participant DB as PostgreSQL
    participant Worker as Worker Evaluator
    participant Domain as Motor de Dominio
    participant Outbox as Outbox Dispatcher
    participant Sender as Notification Sender

    Ingest->>DB: Upsert pronósticos (ON CONFLICT DO UPDATE WHERE issued_at más reciente)
    Note over Worker,DB: Advisory Lock en EvaluateAlerts (pg_try_advisory_xact_lock)
    Worker->>DB: Consulta alertas activas y pronósticos en ventana [hoy, hoy + 7d]
    Worker->>Domain: evaluate(rule, forecast, prev_state)
    Domain-->>Worker: Decision: Fire(episode) / Hold / Rearm / Skip
    alt Decision == Fire(episode)
        Worker->>DB: Guarda AlertBreachState (is_breached=True) + Inserta AlertNotification (status=pending)
    else Decision == Rearm
        Worker->>DB: Actualiza AlertBreachState (is_breached=False)
    end
    Note over Outbox,DB: Lease Pattern con SELECT FOR UPDATE SKIP LOCKED
    Outbox->>DB: Reclama notificaciones pendientes (status=claimed, leased_until=now+lease)
    Outbox->>Sender: send(user_phone, message)
    alt Envío exitoso
        Outbox->>DB: Marca notificación como sent (sent_at=now)
    else Falla de envío
        Outbox->>DB: Incrementa attempts y calcula next_attempt_at con backoff exponencial
    end
```

### Máquina de estados de la alerta
```mermaid
stateDiagram-v2
    [*] --> Armado: Alerta creada / Estado inicial

    Armado --> Disparado: probability >= threshold (Cruza umbral) / Genera Fire(episode=1)
    Disparado --> Disparado: probability >= threshold - REARM_MARGIN (Se mantiene alta) / Hold (no repite aviso)
    Disparado --> Re_armado: probability < threshold - REARM_MARGIN (Baja de umbral) / Rearm
    Re_armado --> Re_armado: probability < threshold (Continúa baja) / Hold
    Re_armado --> Disparado: probability >= threshold (Vuelve a subir) / Genera Fire(episode=episode+1)
```

### Ciclo de vida de una notificación (Outbox pattern)
```mermaid
stateDiagram-v2
    [*] --> pending: Evaluator genera Fire() e inserta registro

    pending --> claimed: Dispatcher adquiere lease con FOR UPDATE SKIP LOCKED
    claimed --> sent: Sender entrega el mensaje exitosamente
    claimed --> pending: Error transitorio (attempts < max_attempts) / Programado con backoff exponencial
    claimed --> failed: Error fatal / Excedió max_attempts / Alerta borrada ('alert_deleted') / Sin teléfono destinatario

    sent --> [*]
    failed --> [*]
```

### Capas del Proyecto (`src/agrobot/`)
- **`domain/`**: Núcleo puro libre de dependencias externas (`evaluator.py`, `entities.py`, `enums.py`, `errors.py`). Modela las entidades de dominio y la función determinística `evaluate()`.
- **`application/`**: Casos de uso (`manage_alerts.py`, `evaluate_alerts.py`, `dispatch_notifications.py`), paginación por cursor (`pagination.py`), utilidades de ETag (`etag.py`) y puertos de abstracción (`ports.py`).
- **`infrastructure/`**: Adaptadores secundarios de base de datos (`db/repositories.py`, `db/models.py`, `db/mappers.py`, `db/uow.py`, `db/locks.py`), reloj (`clock.py`) y mensajería (`notifications/logging_sender.py`).
  - *Ciclo de vida del Advisory Lock*: `PostgresAdvisoryLock` recibe una fábrica de sesiones (`session_factory`). La conexión a Postgres se abre puntualmente al ejecutar `try_acquire()` y se cierra de forma inmediata en `release()` (o si el lock no pudo adquirirse). Esto garantiza que el proceso worker no retenga conexiones en estado `idle in transaction` ni sature el pool durante sus períodos de reposo.
- **`api/`**: Adaptadores primarios HTTP FastAPI (`routers/`, `deps.py`, `schemas.py`, `errors.py`).
- **`worker/`**: Proceso de background daemon (`main.py`, `scheduler.py`).

---

## 3. Cómo correr el proyecto

### Opción A: Docker Compose (Recomendado)

Requiere Docker y Docker Compose instalados.

1. **Configurar variables de entorno**:
   ```bash
   cp .env.example .env
   ```

2. **Levantar los servicios**:
   ```bash
   docker compose up --build -d
   ```
   Esto orquesta de forma totalmente automática:
   - `db`: Base de datos PostgreSQL 16 con healthcheck.
   - `migrate`: Aplica las migraciones de Alembic (`alembic upgrade head`) y finaliza con exit 0.
   - `seed`: Siembra automáticamente los datos iniciales y pronósticos de prueba (el servicio `seed` es exclusivamente para demo y desarrollo local; en producción la tabla de pronósticos la llena el job de ingesta).
   - `api`: Servidor web FastAPI en `http://localhost:8000` (inicia una vez completado el seed).
   - `worker`: Proceso en segundo plano ejecutando la evaluación y el outbox dispatcher (inicia con los datos listos; su primera evaluación se ejecuta al arrancar y en ~30s despacha las notificaciones).

3. **Verificar estado de salud**:
   ```bash
   curl http://localhost:8000/health/ready
   # {"status":"ready"}
   ```

4. **Importar y correr la colección de Postman**:
   Podés importar la colección `postman/agrobot.postman_collection.json` junto con el environment `postman/agrobot.local.postman_environment.json` en Postman, o correr directamente desde la terminal:
   ```bash
   make postman
   ```
   *(Pasa el 100% de los tests sin necesidad de ejecutar ningún comando de seed a mano).*

---

### Opción B: Entorno Local (Desarrollo)

Requiere **Python 3.12** y **PostgreSQL 16**.

1. **Instalar `uv` y dependencias**:
   ```bash
   uv venv
   source .venv/bin/activate
   uv sync --extra dev
   ```

2. **Iniciar base de datos PostgreSQL**:
   Asegurate de que Postgres esté corriendo en el puerto 5432 con usuario `postgres` y contraseña `postgres`:
   ```bash
   createdb agrobot
   createdb agrobot_test  # para los tests
   ```

3. **Ejecutar migraciones**:
   ```bash
   export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot"
   alembic upgrade head
   ```

4. **Sembrar datos iniciales**:
   ```bash
   python scripts/seed_forecasts.py
   ```

5. **Iniciar la API**:
   ```bash
   uvicorn agrobot.api.main:app --host 0.0.0.0 --port 8000 --reload
   ```

6. **Iniciar el Worker (en otra terminal)**:
   ```bash
   python -m agrobot.worker.main
   ```

---

## 4. Probando el Sistema de Punta a Punta

El script `scripts/seed_forecasts.py` puebla la base con los siguientes datos:
- **2 Usuarios**:
  - `11111111-1111-1111-1111-111111111111` ("Juan Perez", Teléfono: `+5491122334455`)
  - `22222222-2222-2222-2222-222222222222` ("Akira Tanaka", Teléfono: `+819012345678`)
- **2 Campos**:
  - `33333333-3333-3333-3333-333333333333` ("Campo Los Molles (Córdoba)", zona horaria `America/Argentina/Cordoba`, propiedad de Juan Perez)
  - `44444444-4444-4444-4444-444444444444` ("Paddy Field (Kanto)", zona horaria `Asia/Tokyo`, propiedad de Akira Tanaka)
- **3 Alertas preexistentes en Córdoba (Juan Perez)**:
  - Alerta 1 (`55555555-5555-5555-5555-555555555555`): Helada (`frost`), umbral 60.00%
  - Alerta 2 (`66666666-6666-6666-6666-666666666666`): Lluvia (`rain`), umbral 50.00%
  - Alerta 3 (`77777777-7777-7777-7777-777777777777`): Lluvia (`rain`), umbral 80.00%

### 1. Listar las alertas existentes
```bash
curl -X GET "http://localhost:8000/api/v1/alerts" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111"
```
Respuesta HTTP 200 OK:
```json
{
  "items": [
    {
      "id": "77777777-7777-7777-7777-777777777777",
      "field_id": "33333333-3333-3333-3333-333333333333",
      "event_type": "rain",
      "threshold": 80.0,
      "status": "active",
      "version": 1,
      "created_at": "2026-05-10T12:00:00Z",
      "updated_at": "2026-05-10T12:00:00Z"
    },
    {
      "id": "66666666-6666-6666-6666-666666666666",
      "field_id": "33333333-3333-3333-3333-333333333333",
      "event_type": "rain",
      "threshold": 50.0,
      "status": "active",
      "version": 1,
      "created_at": "2026-05-10T12:00:00Z",
      "updated_at": "2026-05-10T12:00:00Z"
    },
    {
      "id": "55555555-5555-5555-5555-555555555555",
      "field_id": "33333333-3333-3333-3333-333333333333",
      "event_type": "frost",
      "threshold": 60.0,
      "status": "active",
      "version": 1,
      "created_at": "2026-05-10T12:00:00Z",
      "updated_at": "2026-05-10T12:00:00Z"
    }
  ],
  "next_cursor": null
}
```

### 2. Crear una nueva alerta climática (sin colisión de regla)
Creamos una alerta para ola de calor (`heat_wave`) en "Campo Los Molles" con umbral 35.00%:
```bash
curl -i -X POST "http://localhost:8000/api/v1/alerts" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Idempotency-Key: demo-key-001" \
  -d '{
    "field_id": "33333333-3333-3333-3333-333333333333",
    "event_type": "heat_wave",
    "threshold": 35.00
  }'
```
Respuesta HTTP 201 Created:
```http
HTTP/1.1 201 Created
etag: W/"1"
location: /api/v1/alerts/c3a067ff-2a13-4c07-a369-0df8eef3d4cb
content-type: application/json

{
  "id": "c3a067ff-2a13-4c07-a369-0df8eef3d4cb",
  "field_id": "33333333-3333-3333-3333-333333333333",
  "event_type": "heat_wave",
  "threshold": 35.0,
  "status": "active",
  "version": 1,
  "created_at": "2026-11-15T22:00:00Z",
  "updated_at": "2026-11-15T22:00:00Z"
}
```

### 3. Verificar idempotencia de la petición
Si repetimos exactamente el mismo comando con la misma cabecera `Idempotency-Key`, el backend devuelve la respuesta cacheada sin duplicar registros:
```bash
curl -i -X POST "http://localhost:8000/api/v1/alerts" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Idempotency-Key: demo-key-001" \
  -d '{
    "field_id": "33333333-3333-3333-3333-333333333333",
    "event_type": "heat_wave",
    "threshold": 35.00
  }'
```
Retorna la misma respuesta HTTP 201 Created con el mismo cuerpo e identificadores.

### 4. Observar al Worker evaluando y despachando
El worker procesa periódicamente las evaluaciones y despacha las notificaciones por WhatsApp (simulado). Para observar los envíos en tiempo real:
```bash
docker compose logs -f worker | grep notification_sent_success
```
Salida en el log estructurado:
```text
{"notification_id": "ee7bab9b-2b76-4dc4-ae89-8807cf480796", "phone_number": "+5491122334455", "message": "Alerta meteorológica: riesgo de frost para el 2026-11-16. Probabilidad: 60.00% (umbral configurado: 60.00%).", "attempt": 1, "event": "notification_sent_success", "logger": "notifications.sender", "level": "info", "timestamp": "2026-11-16T01:43:20.274220Z"}
```

### 5. Consultar las notificaciones generadas (Bandeja)
```bash
curl -X GET "http://localhost:8000/api/v1/notifications?limit=10" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111"
```
Respuesta HTTP 200 OK:
```json
{
  "items": [
    {
      "id": "ee7bab9b-2b76-4dc4-ae89-8807cf480796",
      "alert_id": "55555555-5555-5555-5555-555555555555",
      "forecast_date": "2026-11-16",
      "episode": 1,
      "event_type": "frost",
      "probability": 60.0,
      "threshold": 60.0,
      "issued_at": "2026-11-16T00:38:49.704142Z",
      "status": "sent",
      "attempts": 1,
      "created_at": "2026-11-16T01:42:50.176413Z",
      "sent_at": "2026-11-16T01:43:20.274340Z"
    },
    {
      "id": "a3edfc81-d62d-4609-b0e2-fbee52b27538",
      "alert_id": "66666666-6666-6666-6666-666666666666",
      "forecast_date": "2026-11-18",
      "episode": 1,
      "event_type": "rain",
      "probability": 85.0,
      "threshold": 50.0,
      "issued_at": "2026-11-15T23:38:49.704142Z",
      "status": "sent",
      "attempts": 1,
      "created_at": "2026-11-16T01:42:50.176413Z",
      "sent_at": "2026-11-16T01:43:20.274686Z"
    },
    {
      "id": "6342c554-32b2-4a96-ae74-414c73e4e9d8",
      "alert_id": "77777777-7777-7777-7777-777777777777",
      "forecast_date": "2026-11-18",
      "episode": 1,
      "event_type": "rain",
      "probability": 85.0,
      "threshold": 80.0,
      "issued_at": "2026-11-15T23:38:49.704142Z",
      "status": "sent",
      "attempts": 1,
      "created_at": "2026-11-16T01:42:50.176413Z",
      "sent_at": "2026-11-16T01:43:20.274883Z"
    }
  ],
  "next_cursor": null
}
```

---

## 5. Ejecución de Tests y Cobertura

La suite de pruebas incluye:
1. **Tests unitarios**: Máquina de estados pura `evaluate()`, reglas de negocio y casos de uso con fakes en memoria.
2. **Tests de propiedades con Hypothesis**: Comprobación matemática de monotonicidad de episodios e invariantes de probabilidad.
3. **Tests de integración con PostgreSQL real**: Validación de migraciones Alembic, concurrencia en advisory locks, lease pattern con `FOR UPDATE SKIP LOCKED` y condiciones de carrera.
4. **Test de contrato OpenAPI**: Verificación automatizada de consistencia entre el esquema generado por FastAPI y [docs/openapi.yaml](docs/openapi.yaml).

```bash
# Correr toda la suite con reporte de cobertura (exige >= 90% en dominio y aplicación)
make test

# Correr únicamente tests unitarios
make test-unit

# Correr tests de integración y API
make test-integration

# Verificación de linting y tipado estricto
make lint
```

---

## 6. Decisiones Técnicas y Trade-offs

Las decisiones de diseño detalladas se encuentran en [specs/plan.md](specs/plan.md). En resumen:

| Tema | Decisión tomada | Razón principal |
|---|---|---|
| **Deduplicación por episodio** | Máquina de estados en `alert_breach_states` | Previene el spam al productor si el pronóstico se mantiene alto durante días consecutivos, pero re-avisa si baja y vuelve a subir. |
| **Outbox con Lease Pattern** | `FOR UPDATE SKIP LOCKED` con límite de lote | Desacopla el I/O de red del envío de WhatsApp de la transacción de evaluación. Permite escalar workers horizontalmente sin bloqueos. |
| **Control de frescura (`issued_at`)** | `Skip(stale)` si supera 24h | Un pronóstico desactualizado no debe gatillar alarmas falsas ni cambiar el estado de episodios existentes. |
| **Locks distribuidos** | `pg_try_advisory_xact_lock` con gestión de sesión puntual | Evita que réplicas de workers evalúen el mismo lote a la vez, liberando la conexión inmediatamente al terminar la corrida. |
| **Persistencia de Decimales vs JSON** | `PercentageNumber` serializer | Mantiene precisión fija `NUMERIC(5,2)` en Postgres y comparaciones numéricas en Python, mientras genera `type: number` estricto en JSON para cumplir el contrato OpenAPI. |

---

## 7. Qué quedó afuera y por qué

Conforme a lo acordado en [specs/spec.md](specs/spec.md#6-fuera-de-alcance):

1. **Integración real con proveedor de WhatsApp / Twilio**:
   - *Por qué*: El challenge solicita enfocarse en la confiabilidad del motor de alertas y el backend.
   - *Extensión*: Implementar un `TwilioSender` que implemente el protocolo `NotificationSender`.
2. **Autenticación real (OAuth2 / JWT)**:
   - *Por qué*: Se acordó simularla mediante el header `X-User-Id` para simplificar la evaluación.
   - *Extensión*: Sustituir la dependencia `get_current_user_id` en FastAPI por un validador de JWT bearer token; ningún router ni caso de uso se ve afectado gracias a la inyección de dependencias.
3. **Agrupación de múltiples notificaciones en un solo mensaje**:
   - *Por qué*: Es una decisión de UX de canal y no del motor central de alertas.
   - *Extensión*: Puede incorporarse en el `DispatchNotifications` agrupando filas `pending` por `user_id` antes del envío.
4. **Evaluación reactiva post-ingesta (`LISTEN/NOTIFY`)**:
   - *Por qué*: El ciclo periódico por intervalo (cada 15 min) es robusto, simple y suficiente para la frecuencia de actualización de pronósticos meteorológicos (típicamente 6 a 12 horas).
   - *Extensión*: Agregar un listener PostgreSQL que dispare la corrida de `EvaluateAlerts` inmediatamente tras un lote de ingesta.

---

## 8. Comandos útiles del Makefile

- `make install`: Instala el proyecto en modo editable y sus herramientas de desarrollo vía `uv`.
- `make up`: Levanta todo el stack con `docker compose up --build -d`.
- `make down`: Detiene los contenedores.
- `make migrate`: Aplica las migraciones de Alembic a la última versión.
- `make seed`: Puebla la base local con datos de prueba realistas y casos borde.
- `make seed-docker`: Puebla la base dentro de Docker Compose.
- `make test`: Ejecuta todos los tests con verificación de cobertura mínima del 90%.
- `make lint`: Corre `ruff` (linter y format check) y `mypy --strict`.
- `make format`: Autoformatea el código y aplica fixes de linter.
- `make postman`: Ejecuta la suite de pruebas funcionales end-to-end con Newman.
