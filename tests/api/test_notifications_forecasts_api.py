"""Tests para endpoints de notificaciones y pronósticos."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from agrobot.api.deps import get_current_user_id, get_uow
from agrobot.api.main import create_app
from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent
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
    field_id = uuid.uuid4()
    alert_id = uuid.uuid4()
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        user = User(id=user_id, name="Test User", phone_number="+5491122334455")
        field = Field(
            id=field_id,
            owner_id=user_id,
            name="Campo Test",
            latitude=Decimal("-31.42"),
            longitude=Decimal("-64.18"),
            timezone="America/Argentina/Cordoba",
        )
        alert = Alert(
            id=alert_id,
            user_id=user_id,
            field_id=field_id,
            event_type=WeatherEvent.FROST,
            threshold=Decimal("60.00"),
            status=AlertStatus.ACTIVE,
            version=1,
        )
        notif = AlertNotification(
            id=uuid.uuid4(),
            alert_id=alert_id,
            forecast_date=date(2026, 5, 11),
            episode=1,
            event_type=WeatherEvent.FROST,
            probability=Decimal("75.00"),
            threshold=Decimal("60.00"),
            issued_at=now_utc - timedelta(hours=1),
            status=NotificationStatus.SENT,
            attempts=1,
            sent_at=now_utc,
        )
        forecast = WeatherForecast(
            field_id=field_id,
            forecast_date=date(2026, 5, 11),
            event_type=WeatherEvent.FROST,
            probability=Decimal("75.00"),
            issued_at=now_utc - timedelta(hours=1),
        )
        session.add_all([user, field, alert, notif, forecast])
        await session.commit()

    return {"user_id": user_id, "field_id": field_id, "alert_id": alert_id}


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


async def test_list_notifications_inbox(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    headers = {"X-User-Id": str(setup_data["user_id"])}
    r = await client.get("/api/v1/notifications?page=1&page_size=5", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "meta" in data
    assert len(data["items"]) == 1
    assert data["items"][0]["event_type"] == "frost"
    assert data["items"][0]["probability"] == 75.0
    assert data["meta"]["total"] == 1
    assert data["meta"]["page"] == 1
    assert data["meta"]["page_size"] == 5
    assert data["meta"]["total_pages"] == 1
    assert data["meta"]["has_next"] is False
    assert data["meta"]["has_prev"] is False


async def test_list_alert_notifications_history(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    headers = {"X-User-Id": str(setup_data["user_id"])}
    alert_id = setup_data["alert_id"]
    r = await client.get(f"/api/v1/alerts/{alert_id}/notifications", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 1
    assert "meta" in data
    assert data["meta"]["total"] == 1


async def test_list_field_forecasts(
    client: httpx.AsyncClient, setup_data: dict[str, uuid.UUID]
) -> None:
    headers = {"X-User-Id": str(setup_data["user_id"])}
    field_id = setup_data["field_id"]
    r = await client.get(f"/api/v1/fields/{field_id}/forecasts", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["event_type"] == "frost"
    assert data[0]["probability"] == 75.0
