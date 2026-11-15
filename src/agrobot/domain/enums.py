"""Enums del dominio.

Viven en `domain` (no en infraestructura) porque son vocabulario de negocio:
la capa de persistencia los importa, nunca al revés.
"""

from enum import StrEnum


class WeatherEvent(StrEnum):
    """Tipos de evento climático que publica el job de ingesta."""

    RAIN = "rain"
    FROST = "frost"
    HAIL = "hail"
    STRONG_WIND = "strong_wind"
    HEAT_WAVE = "heat_wave"


class AlertStatus(StrEnum):
    """Estado operativo de una alerta. El borrado es lógico (deleted_at), no un estado."""

    ACTIVE = "active"
    PAUSED = "paused"


class NotificationStatus(StrEnum):
    """Ciclo de vida de una notificación en el outbox.

    PENDING -> SENT        (entrega OK)
    PENDING -> FAILED      (se agotaron los reintentos)
    """

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
