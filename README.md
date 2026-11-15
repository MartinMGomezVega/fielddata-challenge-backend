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

El proyecto sigue estrictamente **Clean Architecture**, asegurando que el dominio sea 100% puro e independiente de frameworks de presentación y de persistencia:

```mermaid
flowchart TD
    subgraph Clients["Clientes / Interfaces"]
        CURL["CLI / cURL / Frontend"]
        Cron["APScheduler Worker"]
    end

    subgraph API_Layer["Capa API (FastAPI)"]
        Router["Routers (/alerts, /notifications, /forecasts, /health)"]
        Deps["Dependency Injection (Composition Root)"]
    end

    subgraph Worker_Layer["Capa Worker (APScheduler)"]
        JobEval["EvaluateAlerts Job (Advisory Lock + Batch Keyset)"]
        JobDisp["DispatchNotifications Job (Outbox Lease + Semaphore)"]
    end

    subgraph Application["Capa de Aplicación (Casos de Uso & Puertos)"]
        UsecaseAlerts["ManageAlerts (CRUD)"]
        UsecaseEval["EvaluateAlerts (Orquestación)"]
        UsecaseDisp["DispatchNotifications (Envío)"]
        Ports["Protocols (Repositories, UnitOfWork, Sender, Clock)"]
    end

    subgraph Domain["Capa de Dominio (Núcleo Puro)"]
        Evaluator["evaluate() State Machine"]
        Entities["Entities & Value Objects (AlertRule, ForecastPoint, BreachState)"]
        Decisions["Decisions (Fire, Hold, Rearm, Skip)"]
    end

    subgraph Infrastructure["Capa de Infraestructura (Adaptadores)"]
        SQLA["SQLAlchemy Repositories + UnitOfWork"]
        AdvLocks["Postgres Advisory Locks"]
        OutboxSender["LoggingSender (WhatsApp Mock)"]
        ClockAdapter["SystemClock (UTC)"]
    end

    subgraph Database["Persistencia (PostgreSQL 16)"]
        PG[(Postgres Tables & Enums)]
    end

    CURL --> Router
    Router --> Deps --> UsecaseAlerts
    Cron --> JobEval --> UsecaseEval
    Cron --> JobDisp --> UsecaseDisp

    UsecaseAlerts --> Ports
    UsecaseEval --> Ports
    UsecaseDisp --> Ports

    UsecaseEval --> Evaluator
    Evaluator --> Decisions
    Evaluator --> Entities

    Ports <|.. SQLA
    Ports <|.. AdvLocks
    Ports <|.. OutboxSender
    Ports <|.. ClockAdapter

    SQLA --> PG
    AdvLocks --> PG
```

### Capas del Proyecto (`src/agrobot/`)
- **`domain/`**: Núcleo libre de frameworks (`evaluator.py`, `entities.py`, `enums.py`, `errors.py`). Modela las entidades inmutables y la función determinística `evaluate()`.
- **`application/`**: Casos de uso (`manage_alerts.py`, `evaluate_alerts.py`, `dispatch_notifications.py`) y puertos de abstracción (`ports.py`).
- **`infrastructure/`**: Adaptadores secundarios de base de datos (`db/repositories.py`, `db/models.py`, `db/uow.py`, `db/locks.py`), reloj (`clock.py`) y mensajería (`notifications/logging_sender.py`).
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
   docker compose up --build
   ```
   Esto iniciará:
   - `db`: Base de datos PostgreSQL 16 con healthcheck.
   - `migrate`: Ejecuta `alembic upgrade head` y termina exitosamente.
   - `api`: Servidor web FastAPI en `http://localhost:8000`.
   - `worker`: Proceso en segundo plano ejecutando el evaluador y el despachador.

3. **Verificar estado de salud**:
   ```bash
   curl http://localhost:8000/health/ready
   # {"status":"ready","database":"connected"}
   ```

---

### Opción B: Entorno Local (Desarrollo)

Requiere **Python 3.12** y **PostgreSQL 16**.

1. **Instalar `uv` y dependencias**:
   ```bash
   # Crear entorno virtual e instalar dependencias con soporte de desarrollo
   uv venv
   source .venv/bin/activate
   uv pip install -e ".[dev]"
   ```

2. **Iniciar base de datos PostgreSQL**:
   Asegurate de que Postgres esté corriendo en el puerto 5432 y creá la base de datos:
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

## 4. Prueba de punta a punta (End-to-End con cURL)

El script `scripts/seed_forecasts.py` crea dos usuarios de prueba y tres campos:
- **Usuario A**: `11111111-1111-1111-1111-111111111111`
  - Campo 1 (La Campana, Córdoba): `a1111111-1111-1111-1111-111111111111`
  - Campo 2 (El Triunfo, Córdoba): `a2222222-2222-2222-2222-222222222222`
- **Usuario B**: `22222222-2222-2222-2222-222222222222`
  - Campo 3 (Hokkaido Farm, Tokio): `b1111111-1111-1111-1111-111111111111`

### 1. Sembrar la base de datos
```bash
python scripts/seed_forecasts.py
```
*(Carga pronósticos con probabilidades de lluvia del 90%, 59.99%, casos de borde y fechas futuras).*

### 2. Crear una alerta climática
Creamos una alerta para que nos avise si la probabilidad de lluvia en "La Campana" supera o iguala el **60%**:
```bash
curl -X POST http://localhost:8000/alerts \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Idempotency-Key: demo-key-001" \
  -d '{
    "field_id": "a1111111-1111-1111-1111-111111111111",
    "event_type": "rain",
    "threshold": 60.00
  }'
```
*Respuesta HTTP 201 Created con el ID de la alerta y ETag de versión.*

### 3. Verificar idempotencia de la petición
Si repetimos exactamente el mismo comando con la misma `Idempotency-Key`, obtenemos de inmediato la respuesta cacheada:
```bash
curl -X POST http://localhost:8000/alerts \
  -H "Content-Type: application/json" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111" \
  -H "Idempotency-Key: demo-key-001" \
  -d '{
    "field_id": "a1111111-1111-1111-1111-111111111111",
    "event_type": "rain",
    "threshold": 60.00
  }'
```

### 4. Ejecución del Worker y generación de alertas
El worker en segundo plano (o tu job de evaluación) detecta la alerta activa:
- Compara el umbral de 60% contra los pronósticos sembrados (que contienen fechas con 90% de probabilidad).
- Dispara un `Fire(episode=1)` y escribe el aviso en la tabla `alert_notifications` con estado `pending`.
- El despachador de notificaciones toma el registro con `SKIP LOCKED`, envía el WhatsApp simulado vía `LoggingSender` e interactúa con el estado a `sent`.

### 5. Consultar las notificaciones generadas
```bash
curl -X GET "http://localhost:8000/notifications/inbox?limit=10" \
  -H "X-User-Id: 11111111-1111-1111-1111-111111111111"
```
*Retorna las notificaciones generadas en formato JSON con su estado `sent` y el texto enviado.*

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
| **Locks distribuidos** | `pg_try_advisory_xact_lock` | Evita que dos réplicas del worker evalúen el mismo lote de alertas simultáneamente, sin requerir infraestructura externa como Redis o Zookeeper. |
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
- `make seed`: Puebla la base con datos de prueba realistas y casos borde.
- `make test`: Ejecuta todos los tests con verificación de cobertura mínima del 90%.
- `make lint`: Corre `ruff` (linter y format check) y `mypy --strict`.
- `make format`: Autoformatea el código y aplica fixes de linter.
