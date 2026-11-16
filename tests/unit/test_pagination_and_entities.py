"""Tests unitarios para pagination y entidades de dominio."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agrobot.application.pagination import decode_cursor, encode_cursor
from agrobot.domain.entities import Alert, Field, Forecast
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.errors import InvalidCursor


def test_encode_and_decode_cursor_roundtrip() -> None:
    item_id = uuid.uuid4()
    now = datetime(2026, 5, 10, 14, 30, 0, tzinfo=UTC)
    cursor = encode_cursor(now, item_id)
    assert isinstance(cursor, str)

    decoded_dt, decoded_id = decode_cursor(cursor)
    assert decoded_dt == now
    assert decoded_id == item_id


def test_decode_cursor_invalid_format() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-valid-base64-string!!")

    with pytest.raises(InvalidCursor):
        decode_cursor("invalid_cursor")


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
