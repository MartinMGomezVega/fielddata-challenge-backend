"""Configuración 12-factor del sistema usando pydantic-settings."""

from decimal import Decimal
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Entorno y logging
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Base de datos
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot",
        description="URL de conexión SQLAlchemy async (asyncpg)",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 30.0

    # Dominio y evaluación
    horizon_days: int = Field(
        default=7, description="Ventana de evaluación hacia el futuro en días"
    )
    max_forecast_age_hours: int = Field(
        default=24,
        description="Antigüedad máxima de un pronóstico (issued_at) para considerarlo vigente",
    )
    rearm_margin: Decimal = Field(
        default=Decimal("0.00"),
        description="Margen por debajo del umbral para re-armar la alerta (anti-flapping)",
    )

    # Background workers y jobs
    eval_interval_min: int = Field(
        default=15, description="Intervalo del job de evaluación en minutos"
    )
    dispatch_interval_s: int = Field(
        default=30, description="Intervalo del job de envío de notificaciones en segundos"
    )
    batch_size: int = Field(
        default=500, description="Tamaño de lote para keyset pagination en evaluación"
    )
    max_attempts: int = Field(
        default=3, description="Máximo de reintentos de envío de una notificación"
    )
    lease_duration_s: int = Field(
        default=60, description="Duración del lease de reclamo de notificaciones pendientes"
    )
    dispatch_concurrency: int = Field(
        default=10, description="Concurrencia máxima para envío simultáneo de notificaciones"
    )

    # API
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
