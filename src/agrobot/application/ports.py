"""Puertos (Protocols) de la capa de aplicación.

Define las interfaces abstractas que la capa de infraestructura implementa.
Cumple con el principio de inversión de dependencias de Clean Architecture.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from agrobot.application.pagination import Page, PageParams
from agrobot.domain.entities import (
    Alert,
    BreachState,
    Field,
    Forecast,
    IdempotencyRecord,
    Notification,
)
from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent


class Clock(Protocol):
    """Puerto de tiempo inyectable."""

    def now_utc(self) -> datetime:
        """Devuelve el instante actual en UTC (timezone-aware)."""
        ...


class AlertRepository(Protocol):
    """Puerto de persistencia para reglas de alerta."""

    async def create(self, alert: Alert) -> Alert: ...

    async def get_by_id(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> Alert | None: ...

    async def get_by_id_include_deleted(
        self, alert_id: uuid.UUID, user_id: uuid.UUID
    ) -> Alert | None: ...

    async def get_by_id_system(self, alert_id: uuid.UUID) -> Alert | None: ...

    async def find_active_rules(
        self, batch_size: int, after_id: uuid.UUID | None = None
    ) -> list[Alert]: ...

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        field_id: uuid.UUID | None = None,
        event_type: WeatherEvent | None = None,
        status: AlertStatus | None = None,
        params: PageParams | None = None,
    ) -> Page[Alert]: ...

    async def update(self, alert: Alert) -> Alert: ...

    async def soft_delete(self, alert: Alert) -> None: ...

    async def exists_active(
        self,
        field_id: uuid.UUID,
        event_type: WeatherEvent,
        threshold: Decimal,
        exclude_id: uuid.UUID | None = None,
    ) -> bool: ...


class ForecastRepository(Protocol):
    """Puerto de persistencia para pronósticos meteorológicos."""

    async def upsert(self, forecast: Forecast) -> bool: ...

    async def get_for_field(
        self,
        field_id: uuid.UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        event_type: WeatherEvent | None = None,
    ) -> list[Forecast]: ...

    async def get_for_alerts(
        self,
        field_ids: list[uuid.UUID],
        event_types: list[WeatherEvent],
        from_date: date,
        to_date: date,
    ) -> list[Forecast]: ...


class BreachStateRepository(Protocol):
    """Puerto de persistencia para el estado de cruce de umbral."""

    async def get_states(
        self, alert_ids: list[uuid.UUID], forecast_dates: list[date]
    ) -> dict[tuple[uuid.UUID, date], BreachState]: ...

    async def upsert_state(self, state: BreachState) -> None: ...


class NotificationRepository(Protocol):
    """Puerto de persistencia para notificaciones del outbox."""

    async def create_if_not_exists(self, notification: Notification) -> bool: ...

    async def get_by_alert(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        params: PageParams | None = None,
    ) -> Page[Notification]: ...

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: NotificationStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        params: PageParams | None = None,
    ) -> Page[Notification]: ...

    async def claim_pending(
        self, limit: int, lease_duration: timedelta, now: datetime
    ) -> list[Notification]: ...

    async def update_status(
        self,
        notification_id: uuid.UUID,
        status: NotificationStatus,
        *,
        attempts: int,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
        sent_at: datetime | None = None,
    ) -> None: ...

    async def mark_alert_notifications_failed(
        self, alert_id: uuid.UUID, last_error: str
    ) -> int: ...


class FieldRepository(Protocol):
    """Puerto de persistencia para campos del productor."""

    async def get_by_id(
        self, field_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> Field | None: ...


class IdempotencyRepository(Protocol):
    """Puerto para persistencia y verificación de llaves de idempotencia."""

    async def get(self, user_id: uuid.UUID, key: str) -> IdempotencyRecord | None: ...

    async def save(self, record: IdempotencyRecord) -> None: ...


class NotificationSender(Protocol):
    """Puerto para el canal de mensajería saliente (WhatsApp mock/real)."""

    async def send(self, notification_id: uuid.UUID, phone_number: str, message: str) -> bool: ...


class JobLock(Protocol):
    """Puerto para exclusión mutua distribuida entre réplicas de workers."""

    async def try_acquire(self, lock_id: int) -> bool: ...

    async def release(self, lock_id: int) -> None: ...


class UnitOfWork(Protocol):
    """Patrón Unit of Work para coordinar transacciones atómicas."""

    alerts: AlertRepository
    forecasts: ForecastRepository
    breach_states: BreachStateRepository
    notifications: NotificationRepository
    fields: FieldRepository
    idempotency: IdempotencyRepository

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
