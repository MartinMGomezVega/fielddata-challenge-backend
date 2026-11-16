"""Tests unitarios para pagination y entidades de dominio."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from agrobot.application.pagination import PageMeta, PageParams
from agrobot.domain.entities import Alert, Field, Forecast
from agrobot.domain.enums import AlertStatus, WeatherEvent


def test_page_params_offset() -> None:
    params = PageParams(page=3, page_size=10)
    assert params.offset == 20


def test_page_meta_build() -> None:
    # total=0, page=1, size=10 -> total_pages=0, has_next=False, has_prev=False
    meta_zero = PageMeta.build(total=0, params=PageParams(page=1, page_size=10))
    assert meta_zero.total == 0
    assert meta_zero.page == 1
    assert meta_zero.page_size == 10
    assert meta_zero.total_pages == 0
    assert meta_zero.has_next is False
    assert meta_zero.has_prev is False

    # total=25, page=1, size=10 -> total_pages=3, has_next=True, has_prev=False
    meta_p1 = PageMeta.build(total=25, params=PageParams(page=1, page_size=10))
    assert meta_p1.total == 25
    assert meta_p1.page == 1
    assert meta_p1.page_size == 10
    assert meta_p1.total_pages == 3
    assert meta_p1.has_next is True
    assert meta_p1.has_prev is False

    # total=25, page=3, size=10 -> has_next=False, has_prev=True
    meta_p3 = PageMeta.build(total=25, params=PageParams(page=3, page_size=10))
    assert meta_p3.total == 25
    assert meta_p3.page == 3
    assert meta_p3.total_pages == 3
    assert meta_p3.has_next is False
    assert meta_p3.has_prev is True

    # total=25, page=5, size=10 -> total_pages=3, has_next=False, has_prev=True
    meta_p5 = PageMeta.build(total=25, params=PageParams(page=5, page_size=10))
    assert meta_p5.total == 25
    assert meta_p5.page == 5
    assert meta_p5.total_pages == 3
    assert meta_p5.has_next is False
    assert meta_p5.has_prev is True


def test_alert_is_active_property() -> None:
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    alert_active = Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.RAIN,
        threshold=Decimal("50.00"),
        status=AlertStatus.ACTIVE,
    )
    assert alert_active.is_active is True

    alert_paused = Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.RAIN,
        threshold=Decimal("50.00"),
        status=AlertStatus.PAUSED,
    )
    assert alert_paused.is_active is False

    alert_deleted = Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.RAIN,
        threshold=Decimal("50.00"),
        status=AlertStatus.ACTIVE,
        deleted_at=datetime.now(UTC),
    )
    assert alert_deleted.is_active is False


def test_field_and_forecast_entities() -> None:
    field_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=owner_id,
        name="Lote Sur",
        latitude=Decimal("-31.5"),
        longitude=Decimal("-64.2"),
        timezone="America/Argentina/Cordoba",
    )
    assert field.id == field_id
    assert field.name == "Lote Sur"

    forecast = Forecast(
        field_id=field_id,
        forecast_date=datetime(2026, 5, 11).date(),
        event_type=WeatherEvent.HEAT_WAVE,
        probability=Decimal("85.00"),
        issued_at=datetime.now(UTC),
    )
    assert forecast.probability == Decimal("85.00")
