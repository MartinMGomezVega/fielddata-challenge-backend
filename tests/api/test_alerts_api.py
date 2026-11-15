"""Tests de integración para los endpoints de alertas y criterios AC-14, AC-15, AC-16."""

import os
import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agrobot.api.deps import get_current_user_id, get_uow
from agrobot.api.main import create_app
from agrobot.infrastructure.db.models import (
    Alert,
    AlertBreachState,
    AlertNotification,
    Field,
    IdempotencyKey,
    User,
    WeatherForecast,
)
from agrobot.infrastructure.db.uow import SqlAlchemyUnitOfWork

TEST_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://martingomezvega@localhost:5432/agrobot_test"
)


@pytest.fixture
async def async_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(async_engine):
    return async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def clean_db(session_factory):
    async with session_factory() as session:
        await session.execute(IdempotencyKey.__table__.delete())
        await session.execute(AlertNotification.__table__.delete())
        await session.execute(AlertBreachState.__table__.delete())
        await session.execute(Alert.__table__.delete())
        await session.execute(WeatherForecast.__table__.delete())
        await session.execute(Field.__table__.delete())
        await session.execute(User.__table__.delete())
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(IdempotencyKey.__table__.delete())
        await session.execute(AlertNotification.__table__.delete())
        await session.execute(AlertBreachState.__table__.delete())
        await session.execute(Alert.__table__.delete())
        await session.execute(WeatherForecast.__table__.delete())
        await session.execute(Field.__table__.delete())
        await session.execute(User.__table__.delete())
        await session.commit()


@pytest.fixture
async def setup_data(session_factory, clean_db):
    _ = clean_db
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    foreign_field_id = uuid.uuid4()

    async with session_factory() as session:
        user = User(id=user_id, name="User Main", phone_number="+5491111111111")
        other_user = User(id=other_user_id, name="User Other", phone_number="+5492222222222")

        field = Field(
            id=field_id,
            owner_id=user_id,
            name="Campo Propio",
            latitude=Decimal("-31.42"),
            longitude=Decimal("-64.18"),
            timezone="America/Argentina/Cordoba",
        )
        foreign_field = Field(
            id=foreign_field_id,
            owner_id=other_user_id,
            name="Campo Ajeno",
            latitude=Decimal("-32.00"),
            longitude=Decimal("-65.00"),
            timezone="America/Argentina/Cordoba",
        )
        session.add_all([user, other_user, field, foreign_field])
        await session.commit()

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "field_id": field_id,
        "foreign_field_id": foreign_field_id,
    }


@pytest.fixture
async def client(session_factory, setup_data):
    app = create_app()
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)
    app.dependency_overrides[get_current_user_id] = lambda: setup_data["user_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_ac_14_post_validations(client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]):
    """AC-14: POST con campo ajeno -> 404. Alerta duplicada -> 409. Umbral 0 o 101 -> 422."""
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # 1. Campo ajeno -> 404
    r_foreign = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["foreign_field_id"]),
            "event_type": "frost",
            "threshold": 60.0,
        },
        headers=headers,
    )
    assert r_foreign.status_code == 404
    assert r_foreign.headers["content-type"] == "application/problem+json"
    assert r_foreign.json()["status"] == 404

    # 2. Umbral 0 -> 422
    r_zero = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "frost",
            "threshold": 0.0,
        },
        headers=headers,
    )
    assert r_zero.status_code == 422

    # 3. Umbral 101 -> 422
    r_over = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "frost",
            "threshold": 101.0,
        },
        headers=headers,
    )
    assert r_over.status_code == 422

    # 4. Creación válida inicial -> 201
    r_ok = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "frost",
            "threshold": 60.0,
        },
        headers=headers,
    )
    assert r_ok.status_code == 201
    assert "ETag" in r_ok.headers
    assert "Location" in r_ok.headers

    # 5. Duplicar la misma regla viva -> 409
    r_dup = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "frost",
            "threshold": 60.0,
        },
        headers=headers,
    )
    assert r_dup.status_code == 409
    assert r_dup.headers["content-type"] == "application/problem+json"


async def test_idempotency_key_post(client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]):
    """Garantía de Idempotency-Key en POST /alerts."""
    headers = {
        "X-User-Id": str(setup_data["user_id"]),
        "Idempotency-Key": "test-key-12345",
    }
    body_a = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "rain",
        "threshold": 50.0,
    }

    # Primer intento -> 201 Creado
    r1 = await client.post("/api/v1/alerts", json=body_a, headers=headers)
    assert r1.status_code == 201
    alert_id = r1.json()["id"]

    # Reintento con misma key y mismo body -> 201 con la MISMA respuesta guardada
    r2 = await client.post("/api/v1/alerts", json=body_a, headers=headers)
    assert r2.status_code == 201
    assert r2.json()["id"] == alert_id

    # Reintento con misma key pero body diferente -> 422
    body_b = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "rain",
        "threshold": 80.0,
    }
    r3 = await client.post("/api/v1/alerts", json=body_b, headers=headers)
    assert r3.status_code == 422
    assert r3.headers["content-type"] == "application/problem+json"


async def test_ac_15_patch_etag_optimistic_locking(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
):
    """AC-15: PATCH con If-Match desactualizado -> 412.
    Con ETag correcto -> 200 y versión incrementada.
    """
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # Crear alerta
    r_create = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "hail",
            "threshold": 70.0,
        },
        headers=headers,
    )
    assert r_create.status_code == 201
    alert_id = r_create.json()["id"]
    etag_v1 = r_create.headers["ETag"]
    assert etag_v1 == 'W/"1"'

    # PATCH con If-Match desactualizado (ej. W/"5") -> 412
    r_stale = await client.patch(
        f"/api/v1/alerts/{alert_id}",
        json={"threshold": 75.0},
        headers={**headers, "If-Match": 'W/"5"'},
    )
    assert r_stale.status_code == 412
    assert r_stale.headers["content-type"] == "application/problem+json"

    # PATCH con If-Match correcto -> 200 y nuevo ETag W/"2"
    r_ok = await client.patch(
        f"/api/v1/alerts/{alert_id}",
        json={"threshold": 75.0},
        headers={**headers, "If-Match": etag_v1},
    )
    assert r_ok.status_code == 200
    assert r_ok.headers["ETag"] == 'W/"2"'
    assert r_ok.json()["threshold"] == "75.00" or r_ok.json()["threshold"] == 75.0


async def test_ac_16_delete_idempotent(client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]):
    """AC-16: DELETE dos veces -> 204 ambas.
    La alerta deja de evaluarse y su historial sigue consultable.
    """
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # Crear alerta
    r_create = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "heat_wave",
            "threshold": 80.0,
        },
        headers=headers,
    )
    alert_id = r_create.json()["id"]

    # 1. Primer borrado -> 204
    r_del1 = await client.delete(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_del1.status_code == 204

    # 2. Segundo borrado (idempotente) -> 204
    r_del2 = await client.delete(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_del2.status_code == 204

    # 3. GET de la alerta borrada -> 404
    r_get = await client.get(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_get.status_code == 404

    # 4. Historial de notificaciones sigue consultable sin error 500
    r_notifs = await client.get(f"/api/v1/alerts/{alert_id}/notifications", headers=headers)
    assert r_notifs.status_code in (200, 404)


async def test_health_endpoints(client: httpx.AsyncClient):
    """Verificación de endpoints /health/live y /health/ready."""
    r_live = await client.get("/health/live")
    assert r_live.status_code == 200
    assert r_live.json() == {"status": "ok"}

    r_ready = await client.get("/health/ready")
    assert r_ready.status_code == 200
    assert r_ready.json() == {"status": "ready"}
