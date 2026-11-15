"""Puertos (Protocols) de la capa de aplicación.

Define las interfaces abstractas que la capa de infraestructura implementa.
Cumple con el principio de inversión de dependencias de Clean Architecture.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent
from agrobot.infrastructure.db.models import (
    Alert,
    AlertBreachState,
    AlertNotification,
    Field,
    IdempotencyKey,
    WeatherForecast,
)


class Clock(Protocol):
    """Puerto de tiempo inyectable."""

    def now_utc(self) -> datetime:
        """Devuelve el instante actual en UTC (timezone-aware)."""
        ...


class AlertRepository(Protocol):
    """Puerto de persistencia para reglas de alerta."""

    async def create(self, alert: Alert) -> Alert: ...

    async def get_by_id(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> Alert | None: ...

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
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Alert], str | None]: ...

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

    async def upsert(self, forecast: WeatherForecast) -> bool: ...

    async def get_for_field(
        self,
        field_id: uuid.UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        event_type: WeatherEvent | None = None,
    ) -> list[WeatherForecast]: ...

    async def get_for_alerts(
        self,
        field_ids: list[uuid.UUID],
        event_types: list[WeatherEvent],
        from_date: date,
        to_date: date,
    ) -> list[WeatherForecast]: ...


class BreachStateRepository(Protocol):
    """Puerto de persistencia para el estado de cruce de umbral."""

    async def get_states(
        self, alert_ids: list[uuid.UUID], forecast_dates: list[date]
    ) -> dict[tuple[uuid.UUID, date], AlertBreachState]: ...

    async def upsert_state(self, state: AlertBreachState) -> None: ...


class NotificationRepository(Protocol):
    """Puerto de persistencia para outbox de notificaciones."""

    async def create_if_not_exists(self, notification: AlertNotification) -> bool:
        """Inserta con ON CONFLICT (alert_id, forecast_date, episode) DO NOTHING."""
        ...

    async def get_by_alert(
        self, alert_id: uuid.UUID, user_id: uuid.UUID, *, limit: int = 20, cursor: str | None = None
    ) -> tuple[list[AlertNotification], str | None]: ...

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: NotificationStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[AlertNotification], str | None]: ...

    async def claim_pending(
        self, limit: int, lease_duration: timedelta
    ) -> list[AlertNotification]: ...

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


class FieldRepository(Protocol):
    """Puerto de consulta de campos."""

    async def get_by_id(
        self, field_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> Field | None: ...


class IdempotencyRepository(Protocol):
    """Puerto para deduplicación de mutaciones HTTP mediante Idempotency-Key."""

    async def get(self, user_id: uuid.UUID, key: str) -> IdempotencyKey | None: ...

    async def save(self, record: IdempotencyKey) -> None: ...


class NotificationSender(Protocol):
    """Puerto para envío externo de notificaciones (ej. WhatsApp)."""

    async def send(
        self,
        notification_id: uuid.UUID,
        phone_number: str,
        message: str,
    ) -> bool: ...


class JobLock(Protocol):
    """Puerto para exclusión mutua distribuida entre réplicas del worker."""

    async def try_acquire(self, lock_id: int) -> bool: ...

    async def release(self, lock_id: int) -> None: ...


class UnitOfWork(Protocol):
    """Unidad de trabajo (Unit of Work) para transacciones compuestas."""

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
