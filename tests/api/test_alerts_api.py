"""Tests de integración para los endpoints de alertas y criterios AC-14, AC-15, AC-16."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agrobot.api.deps import get_current_user_id, get_uow
from agrobot.api.main import create_app
from agrobot.application.use_cases.evaluate_alerts import EvaluateAlerts
from agrobot.domain.enums import NotificationStatus, WeatherEvent
from agrobot.infrastructure.clock import FrozenClock
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


@pytest.fixture
async def clean_db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with session_factory() as session:
        await session.execute(delete(IdempotencyKey))
        await session.execute(delete(AlertNotification))
        await session.execute(delete(AlertBreachState))
        await session.execute(delete(Alert))
        await session.execute(delete(WeatherForecast))
        await session.execute(delete(Field))
        await session.execute(delete(User))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(delete(IdempotencyKey))
        await session.execute(delete(AlertNotification))
        await session.execute(delete(AlertBreachState))
        await session.execute(delete(Alert))
        await session.execute(delete(WeatherForecast))
        await session.execute(delete(Field))
        await session.execute(delete(User))
        await session.commit()


@pytest.fixture
async def setup_data(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> dict[str, uuid.UUID]:
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
async def client(
    session_factory: async_sessionmaker[AsyncSession], setup_data: dict[str, uuid.UUID]
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)
    app.dependency_overrides[get_current_user_id] = lambda: setup_data["user_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_ac_14_post_validations(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
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


async def test_idempotency_key_post(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
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
) -> None:
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
    assert r_ok.json()["threshold"] in ("75.00", 75.0)

    # PATCH sin campos en el cuerpo -> 422
    r_empty = await client.patch(
        f"/api/v1/alerts/{alert_id}",
        json={},
        headers={**headers, "If-Match": 'W/"2"'},
    )
    assert r_empty.status_code == 422
    assert r_empty.headers["content-type"] == "application/problem+json"


async def test_ac_16_delete_idempotent(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    setup_data: dict[str, uuid.UUID],
) -> None:
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
    alert_id = uuid.UUID(r_create.json()["id"])

    # Sembrar una notificación pasada para esta alerta
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    notif_id = uuid.uuid4()
    async with session_factory() as session:
        notif = AlertNotification(
            id=notif_id,
            alert_id=alert_id,
            forecast_date=date(2026, 5, 12),
            episode=1,
            event_type=WeatherEvent.HEAT_WAVE,
            probability=Decimal("85.00"),
            threshold=Decimal("80.00"),
            issued_at=now_utc - timedelta(hours=2),
            status=NotificationStatus.SENT,
            attempts=1,
            next_attempt_at=now_utc,
            sent_at=now_utc - timedelta(hours=1),
        )
        session.add(notif)
        await session.commit()

    # 1. Primer borrado -> 204
    r_del1 = await client.delete(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_del1.status_code == 204

    # 2. Segundo borrado (idempotente) -> 204
    r_del2 = await client.delete(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_del2.status_code == 204

    # 3. GET de la alerta borrada -> 404
    r_get = await client.get(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_get.status_code == 404

    # 4. Historial de notificaciones sigue consultable -> 200 con sus items (AC-16)
    r_notifs = await client.get(f"/api/v1/alerts/{alert_id}/notifications", headers=headers)
    assert r_notifs.status_code == 200
    data = r_notifs.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == str(notif_id)

    # 5. La bandeja general de notificaciones del usuario también la incluye
    r_inbox = await client.get("/api/v1/notifications", headers=headers)
    assert r_inbox.status_code == 200
    assert any(item["id"] == str(notif_id) for item in r_inbox.json()["items"])

    # 6. El evaluador ya no evalúa la alerta borrada
    uow = SqlAlchemyUnitOfWork(session_factory)
    clock = FrozenClock(now_utc)
    evaluator = EvaluateAlerts(
        uow=uow,
        clock=clock,
        job_lock=None,
    )
    report = await evaluator.run()
    assert report.alerts_evaluated == 0
    assert report.fired == 0


async def test_delete_alert_cancels_pending_notifications(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    setup_data: dict[str, uuid.UUID],
) -> None:
    """Bloque 4 Bug 7: Al borrar una alerta, sus notificaciones pending pasan
    a failed ('alert_deleted').
    """
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # Crear alerta
    r_create = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "strong_wind",
            "threshold": 45.0,
        },
        headers=headers,
    )
    assert r_create.status_code == 201
    alert_id = uuid.UUID(r_create.json()["id"])

    # Sembrar notificación en estado PENDING
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    notif_id = uuid.uuid4()
    async with session_factory() as session:
        notif = AlertNotification(
            id=notif_id,
            alert_id=alert_id,
            forecast_date=date(2026, 5, 13),
            episode=1,
            event_type=WeatherEvent.STRONG_WIND,
            probability=Decimal("50.00"),
            threshold=Decimal("45.00"),
            issued_at=now_utc - timedelta(hours=1),
            status=NotificationStatus.PENDING,
            attempts=0,
            next_attempt_at=now_utc,
        )
        session.add(notif)
        await session.commit()

    # Borrar la alerta
    r_del = await client.delete(f"/api/v1/alerts/{alert_id}", headers=headers)
    assert r_del.status_code == 204

    # Verificar que en la base la notificación pasó a FAILED con last_error = 'alert_deleted'
    async with session_factory() as session:
        updated_notif = await session.get(AlertNotification, notif_id)
        assert updated_notif is not None
        assert updated_notif.status == NotificationStatus.FAILED
        assert updated_notif.last_error == "alert_deleted"


async def test_list_alerts_filter_by_status(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    """Bloque 4 Bug 3: GET /api/v1/alerts?status=paused filtra correctamente."""
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # Crear alerta 1 y pausarla
    r1 = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "frost",
            "threshold": 30.0,
        },
        headers=headers,
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]
    etag1 = r1.headers["ETag"]

    r_patch = await client.patch(
        f"/api/v1/alerts/{id1}",
        json={"status": "paused"},
        headers={**headers, "If-Match": etag1},
    )
    assert r_patch.status_code == 200

    # Crear alerta 2 (activa)
    r2 = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "rain",
            "threshold": 40.0,
        },
        headers=headers,
    )
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    # Filtrar por status=paused
    r_paused = await client.get("/api/v1/alerts?status=paused", headers=headers)
    assert r_paused.status_code == 200
    paused_ids = [a["id"] for a in r_paused.json()["items"]]
    assert id1 in paused_ids
    assert id2 not in paused_ids

    # Filtrar por status=active
    r_active = await client.get("/api/v1/alerts?status=active", headers=headers)
    assert r_active.status_code == 200
    active_ids = [a["id"] for a in r_active.json()["items"]]
    assert id2 in active_ids
    assert id1 not in active_ids


async def test_list_alerts_pagination_and_validation(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    """Paginación por número de página y validaciones de rango."""
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # Crear 12 alertas para un usuario con umbrales distintos
    created_ids: list[str] = []
    for i in range(12):
        r = await client.post(
            "/api/v1/alerts",
            json={
                "field_id": str(setup_data["field_id"]),
                "event_type": "frost",
                "threshold": float(10 + i),
            },
            headers=headers,
        )
        assert r.status_code == 201
        created_ids.append(r.json()["id"])

    # GET ?page=1 -> 10 items, meta.total == 12, total_pages == 2, has_next True, has_prev False
    r_p1 = await client.get("/api/v1/alerts?page=1", headers=headers)
    assert r_p1.status_code == 200
    data_p1 = r_p1.json()
    assert len(data_p1["items"]) == 10
    assert data_p1["meta"]["total"] == 12
    assert data_p1["meta"]["page"] == 1
    assert data_p1["meta"]["page_size"] == 10
    assert data_p1["meta"]["total_pages"] == 2
    assert data_p1["meta"]["has_next"] is True
    assert data_p1["meta"]["has_prev"] is False

    # El orden es del más nuevo al más viejo (created_at DESC, id DESC)
    p1_ids = [a["id"] for a in data_p1["items"]]
    assert p1_ids == list(reversed(created_ids[-10:]))

    # GET ?page=2 -> 2 items, has_next False, has_prev True; ningún id repetido
    r_p2 = await client.get("/api/v1/alerts?page=2", headers=headers)
    assert r_p2.status_code == 200
    data_p2 = r_p2.json()
    assert len(data_p2["items"]) == 2
    assert data_p2["meta"]["total"] == 12
    assert data_p2["meta"]["page"] == 2
    assert data_p2["meta"]["has_next"] is False
    assert data_p2["meta"]["has_prev"] is True
    p2_ids = [a["id"] for a in data_p2["items"]]
    assert set(p1_ids).isdisjoint(set(p2_ids))

    # GET ?page=3 -> 200, items == [], meta.total == 12
    r_p3 = await client.get("/api/v1/alerts?page=3", headers=headers)
    assert r_p3.status_code == 200
    data_p3 = r_p3.json()
    assert data_p3["items"] == []
    assert data_p3["meta"]["total"] == 12
    assert data_p3["meta"]["page"] == 3
    assert data_p3["meta"]["total_pages"] == 2
    assert data_p3["meta"]["has_next"] is False
    assert data_p3["meta"]["has_prev"] is True

    # GET ?page_size=5 -> 5 items, total_pages == 3
    r_s5 = await client.get("/api/v1/alerts?page_size=5", headers=headers)
    assert r_s5.status_code == 200
    data_s5 = r_s5.json()
    assert len(data_s5["items"]) == 5
    assert data_s5["meta"]["total_pages"] == 3

    # ?page=0, ?page_size=0 y ?page_size=101 -> 422 con application/problem+json
    for query_param in ("page=0", "page_size=0", "page_size=101"):
        r_err = await client.get(f"/api/v1/alerts?{query_param}", headers=headers)
        assert r_err.status_code == 422
        assert r_err.headers["content-type"] == "application/problem+json"


async def test_concurrent_posts_and_idempotency_race(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    setup_data: dict[str, uuid.UUID],
) -> None:
    """Bloque 4 Bug 5: Concurrencia en POST /alerts.

    - Dos POST idénticos simultáneos: uno 201, el otro 409 (DuplicateAlert).
    - Dos POST simultáneos con misma Idempotency-Key: ambos 201 con mismo id y una sola fila en DB.
    - Reuso de Idempotency-Key vencida: genera 201 nuevo.
    """
    headers = {"X-User-Id": str(setup_data["user_id"])}

    # 1. Dos POST idénticos sin key simultáneos -> uno 201, otro 409
    body_dup = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "hail",
        "threshold": 65.0,
    }
    r_a, r_b = await asyncio.gather(
        client.post("/api/v1/alerts", json=body_dup, headers=headers),
        client.post("/api/v1/alerts", json=body_dup, headers=headers),
    )
    statuses = {r_a.status_code, r_b.status_code}
    assert statuses == {201, 409}

    # 2. Dos POST simultáneos con la MISMA Idempotency-Key
    idem_key = f"race-key-{uuid.uuid4()}"
    headers_idem = {**headers, "Idempotency-Key": idem_key}
    body_idem = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "heat_wave",
        "threshold": 38.0,
    }
    r_id1, r_id2 = await asyncio.gather(
        client.post("/api/v1/alerts", json=body_idem, headers=headers_idem),
        client.post("/api/v1/alerts", json=body_idem, headers=headers_idem),
    )
    assert r_id1.status_code == 201
    assert r_id2.status_code == 201
    assert r_id1.json()["id"] == r_id2.json()["id"]

    # Verificar que solo hay 1 alerta en la base para heat_wave
    async with session_factory() as session:
        res = await session.execute(
            delete(Alert).where(
                Alert.event_type == WeatherEvent.HEAT_WAVE,
                Alert.threshold == Decimal("38.00"),
            )
        )
        assert getattr(res, "rowcount", 0) == 1

    # 3. Reuso de Idempotency-Key vencida -> 201 nuevo reemplazando la clave
    expired_key = f"expired-key-{uuid.uuid4()}"
    headers_exp = {**headers, "Idempotency-Key": expired_key}
    body_exp1 = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "strong_wind",
        "threshold": 55.0,
    }
    r_exp1 = await client.post("/api/v1/alerts", json=body_exp1, headers=headers_exp)
    assert r_exp1.status_code == 201

    # Forzar expiración de la clave en la base
    async with session_factory() as session:
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key == expired_key)
            .values(expires_at=func.now() - timedelta(hours=1))
        )
        await session.commit()

    # Reutilizar la clave vencida para una nueva alerta
    body_exp2 = {
        "field_id": str(setup_data["field_id"]),
        "event_type": "frost",
        "threshold": 92.0,
    }
    r_exp2 = await client.post("/api/v1/alerts", json=body_exp2, headers=headers_exp)
    assert r_exp2.status_code == 201
    assert r_exp2.json()["id"] != r_exp1.json()["id"]


async def test_patch_malformed_if_match_returns_412(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    """Bloque 4 Bug 5: If-Match malformado devuelve 412 Precondition Failed, no se ignora."""
    headers = {"X-User-Id": str(setup_data["user_id"])}

    r_create = await client.post(
        "/api/v1/alerts",
        json={
            "field_id": str(setup_data["field_id"]),
            "event_type": "rain",
            "threshold": 77.0,
        },
        headers=headers,
    )
    assert r_create.status_code == 201
    alert_id = r_create.json()["id"]

    for bad_etag in ("not-an-etag", 'W/"xyz"', '""', "123"):
        r_bad = await client.patch(
            f"/api/v1/alerts/{alert_id}",
            json={"threshold": 78.0},
            headers={**headers, "If-Match": bad_etag},
        )
        assert r_bad.status_code == 412
        assert r_bad.headers["content-type"] == "application/problem+json"


async def test_rfc9457_error_responses(
    session_factory: async_sessionmaker[AsyncSession], setup_data: dict[str, uuid.UUID]
) -> None:
    """Bloque 4 Bug 11: X-User-Id ausente o inválido, y 404/500 devuelven
    application/problem+json.
    """
    app = create_app()
    app.dependency_overrides[get_uow] = lambda: SqlAlchemyUnitOfWork(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as unauth_client:
        # 1. Header X-User-Id ausente -> 422 problem+json
        r_missing_auth = await unauth_client.get("/api/v1/alerts")
        assert r_missing_auth.status_code == 422
        assert r_missing_auth.headers["content-type"] == "application/problem+json"
        assert r_missing_auth.json()["title"] == "Validation Error"

        # 2. Header X-User-Id con formato inválido -> 422 problem+json
        r_bad_auth = await unauth_client.get("/api/v1/alerts", headers={"X-User-Id": "not-a-uuid"})
        assert r_bad_auth.status_code == 422
        assert r_bad_auth.headers["content-type"] == "application/problem+json"
        assert "Formato de X-User-Id inválido" in r_bad_auth.json()["detail"]

        # 3. Endpoint inexistente -> 404 problem+json
        r_404 = await unauth_client.get(
            "/api/v1/nonexistent", headers={"X-User-Id": str(setup_data["user_id"])}
        )
        assert r_404.status_code == 404
        assert r_404.headers["content-type"] == "application/problem+json"
        assert r_404.json()["title"] == "Not Found"


async def test_health_endpoints(client: httpx.AsyncClient) -> None:
    """Verificación de endpoints /health/live y /health/ready."""
    r_live = await client.get("/health/live")
    assert r_live.status_code == 200
    assert r_live.json() == {"status": "ok"}

    r_ready = await client.get("/health/ready")
    assert r_ready.status_code == 200
    assert r_ready.json() == {"status": "ready"}
