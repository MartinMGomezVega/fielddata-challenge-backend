"""Entidades puras del dominio (dataclasses frozen).

Sin dependencias de SQLAlchemy, FastAPI ni I/O.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent


@dataclass(frozen=True)
class AlertRule:
    """Representación de dominio de una regla de alerta configurada."""

    id: uuid.UUID
    field_id: uuid.UUID
    user_id: uuid.UUID
    event_type: WeatherEvent
    threshold: Decimal
    timezone: ZoneInfo
    status: AlertStatus = AlertStatus.ACTIVE
    deleted_at: datetime | None = None
    version: int = 1

    @property
    def is_active(self) -> bool:
        return self.status == AlertStatus.ACTIVE and self.deleted_at is None


@dataclass(frozen=True)
class ForecastPoint:
    """Punto de pronóstico climático para un campo, fecha y evento."""

    forecast_date: date
    event_type: WeatherEvent
    probability: Decimal
    issued_at: datetime
    source: str = "mock"


@dataclass(frozen=True)
class BreachState:
    """Estado persistido del cruce de umbral para una (alerta, forecast_date)."""

    alert_id: uuid.UUID
    forecast_date: date
    is_breached: bool
    episode: int
    last_probability: Decimal
    last_issued_at: datetime


@dataclass(frozen=True)
class NotificationRecord:
    """Registro de notificación generado para el outbox."""

    id: uuid.UUID
    alert_id: uuid.UUID
    forecast_date: date
    episode: int
    event_type: WeatherEvent
    probability: Decimal
    threshold: Decimal
    issued_at: datetime
    status: NotificationStatus = NotificationStatus.PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    sent_at: datetime | None = None
    created_at: datetime | None = None


# Decisiones del evaluador (Algebraic Data Types)
@dataclass(frozen=True)
class Skip:
    reason: str  # "out_of_window" | "stale" | "inactive_rule" | "event_mismatch"


@dataclass(frozen=True)
class Hold:
    """La probabilidad sigue por encima del umbral; no se re-avisa."""


@dataclass(frozen=True)
class Rearm:
    """La probabilidad bajó del umbral (- REARM_MARGIN); se re-arma para el próximo cruce."""


@dataclass(frozen=True)
class Fire:
    """Cruce hacia arriba del umbral; se debe emitir notificación."""

    episode: int


Decision = Skip | Hold | Rearm | Fire
