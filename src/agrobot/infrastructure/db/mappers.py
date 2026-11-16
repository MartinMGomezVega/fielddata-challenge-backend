"""Mappers bidireccionales entre modelos ORM SQLAlchemy y entidades puras de dominio."""

from __future__ import annotations

from sqlalchemy import inspect as sa_inspect

from agrobot.domain.entities import (
    Alert as AlertEntity,
)
from agrobot.domain.entities import (
    BreachState as BreachStateEntity,
)
from agrobot.domain.entities import (
    Field as FieldEntity,
)
from agrobot.domain.entities import (
    Forecast as ForecastEntity,
)
from agrobot.domain.entities import (
    IdempotencyRecord as IdempotencyEntity,
)
from agrobot.domain.entities import (
    Notification as NotificationEntity,
)
from agrobot.infrastructure.db.models import (
    Alert as AlertModel,
)
from agrobot.infrastructure.db.models import (
    AlertBreachState as AlertBreachStateModel,
)
from agrobot.infrastructure.db.models import (
    AlertNotification as AlertNotificationModel,
)
from agrobot.infrastructure.db.models import (
    Field as FieldModel,
)
from agrobot.infrastructure.db.models import (
    IdempotencyKey as IdempotencyKeyModel,
)
from agrobot.infrastructure.db.models import (
    WeatherForecast as WeatherForecastModel,
)


def to_field_entity(model: FieldModel) -> FieldEntity:
    return FieldEntity(
        id=model.id,
        owner_id=model.owner_id,
        name=model.name,
        latitude=model.latitude,
        longitude=model.longitude,
        timezone=model.timezone,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def to_field_model(entity: FieldEntity) -> FieldModel:
    return FieldModel(
        id=entity.id,
        owner_id=entity.owner_id,
        name=entity.name,
        latitude=entity.latitude,
        longitude=entity.longitude,
        timezone=entity.timezone,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def to_alert_entity(model: AlertModel) -> AlertEntity:
    field_entity = None
    insp = sa_inspect(model)
    if "field" not in insp.unloaded and model.field is not None:
        field_entity = to_field_entity(model.field)

    return AlertEntity(
        id=model.id,
        field_id=model.field_id,
        user_id=model.user_id,
        event_type=model.event_type,
        threshold=model.threshold,
        status=model.status,
        version=model.version,
        deleted_at=model.deleted_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        field=field_entity,
    )


def to_alert_model(entity: AlertEntity) -> AlertModel:
    return AlertModel(
        id=entity.id,
        field_id=entity.field_id,
        user_id=entity.user_id,
        event_type=entity.event_type,
        threshold=entity.threshold,
        status=entity.status,
        version=entity.version,
        deleted_at=entity.deleted_at,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def to_forecast_entity(model: WeatherForecastModel) -> ForecastEntity:
    return ForecastEntity(
        id=model.id,
        field_id=model.field_id,
        forecast_date=model.forecast_date,
        event_type=model.event_type,
        probability=model.probability,
        issued_at=model.issued_at,
        source=model.source,
        ingested_at=model.ingested_at,
    )


def to_forecast_model(entity: ForecastEntity) -> WeatherForecastModel:
    kwargs: dict[str, object] = {
        "field_id": entity.field_id,
        "forecast_date": entity.forecast_date,
        "event_type": entity.event_type,
        "probability": entity.probability,
        "issued_at": entity.issued_at,
        "source": entity.source,
    }
    if entity.id is not None:
        kwargs["id"] = entity.id
    if entity.ingested_at is not None:
        kwargs["ingested_at"] = entity.ingested_at
    return WeatherForecastModel(**kwargs)


def to_breach_state_entity(model: AlertBreachStateModel) -> BreachStateEntity:
    return BreachStateEntity(
        alert_id=model.alert_id,
        forecast_date=model.forecast_date,
        is_breached=model.is_breached,
        episode=model.episode,
        last_probability=model.last_probability,
        last_issued_at=model.last_issued_at,
    )


def to_breach_state_model(entity: BreachStateEntity) -> AlertBreachStateModel:
    return AlertBreachStateModel(
        alert_id=entity.alert_id,
        forecast_date=entity.forecast_date,
        is_breached=entity.is_breached,
        episode=entity.episode,
        last_probability=entity.last_probability,
        last_issued_at=entity.last_issued_at,
    )


def to_notification_entity(model: AlertNotificationModel) -> NotificationEntity:
    phone: str | None = None
    insp = sa_inspect(model)
    if "alert" not in insp.unloaded and model.alert is not None:
        alert = model.alert
        alert_insp = sa_inspect(alert)
        if "user" not in alert_insp.unloaded and alert.user is not None:
            phone = alert.user.phone_number

    return NotificationEntity(
        id=model.id,
        alert_id=model.alert_id,
        forecast_date=model.forecast_date,
        episode=model.episode,
        event_type=model.event_type,
        probability=model.probability,
        threshold=model.threshold,
        issued_at=model.issued_at,
        status=model.status,
        attempts=model.attempts,
        next_attempt_at=model.next_attempt_at,
        last_error=model.last_error,
        sent_at=model.sent_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
        recipient_phone=phone,
    )


def to_notification_model(entity: NotificationEntity) -> AlertNotificationModel:
    return AlertNotificationModel(
        id=entity.id,
        alert_id=entity.alert_id,
        forecast_date=entity.forecast_date,
        episode=entity.episode,
        event_type=entity.event_type,
        probability=entity.probability,
        threshold=entity.threshold,
        issued_at=entity.issued_at,
        status=entity.status,
        attempts=entity.attempts,
        next_attempt_at=entity.next_attempt_at,
        last_error=entity.last_error,
        sent_at=entity.sent_at,
        created_at=entity.created_at,
        updated_at=entity.updated_at,
    )


def to_idempotency_entity(model: IdempotencyKeyModel) -> IdempotencyEntity:
    return IdempotencyEntity(
        id=model.id,
        user_id=model.user_id,
        key=model.key,
        request_hash=model.request_hash,
        response_code=model.response_code,
        response_headers=model.response_headers,
        response_body=model.response_body,
        created_at=model.created_at,
        expires_at=model.expires_at,
    )


def to_idempotency_model(entity: IdempotencyEntity) -> IdempotencyKeyModel:
    return IdempotencyKeyModel(
        id=entity.id,
        user_id=entity.user_id,
        key=entity.key,
        request_hash=entity.request_hash,
        response_code=entity.response_code,
        response_headers=entity.response_headers,
        response_body=entity.response_body,
        created_at=entity.created_at,
        expires_at=entity.expires_at,
    )
