"""Modelos SQLAlchemy 2.0 (typed, `Mapped[...]`).

Decisiones clave (detalle en Specs/plan.md §2):
- PKs UUID: no exponen volumen ni orden, seguros para IDs públicos en la API.
- Probabilidad y umbral como NUMERIC(5,2) en escala 0-100: exactos (sin errores de
  float al comparar >=) y con CHECK en la base, no sólo en Pydantic.
- Instantes en TIMESTAMPTZ (UTC). `forecast_date` es DATE en la hora LOCAL DEL CAMPO
  (`fields.timezone`): "hoy" se calcula por campo, no por servidor.
- Idempotencia garantizada por constraints, no por lógica de aplicación:
    * weather_forecasts: UNIQUE(field_id, forecast_date, event_type) -> upsert del ingestor,
      que sólo pisa si `issued_at` es más nuevo (reintentos y llegadas desordenadas no
      retroceden el dato).
    * alert_breach_states: PK(alert_id, forecast_date) -> máquina de estados armada/disparada.
    * alert_notifications: UNIQUE(alert_id, forecast_date, episode) -> 1 aviso por cada
      vez que el pronóstico CRUZA el umbral hacia arriba (re-aviso si baja y vuelve a subir).
    * idempotency_keys: UNIQUE(user_id, key) -> deduplicación de requests POST /alerts.
- Borrado lógico de alertas (deleted_at) para no romper la FK del historial de avisos.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent

# Convención de nombres: Alembic genera nombres de constraints deterministas,
# así los downgrade/upgrade no dependen de nombres autogenerados por Postgres.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _get_enum_values(enum_type: type[enum.Enum]) -> list[object]:
    return [m.value for m in enum_type]


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # values_callable -> se persiste el value ("rain"), no el nombre ("RAIN").
    return Enum(
        enum_cls,
        name=name,
        values_callable=_get_enum_values,
        validate_strings=True,
    )


WEATHER_EVENT_ENUM = _pg_enum(WeatherEvent, "weather_event")
ALERT_STATUS_ENUM = _pg_enum(AlertStatus, "alert_status")
NOTIFICATION_STATUS_ENUM = _pg_enum(NotificationStatus, "notification_status")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Contexto mínimo (en Agrobot real ya existen; acá los modelamos lo justo).
# --------------------------------------------------------------------------- #
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # E.164: es el "canal" con el que se notificaría por WhatsApp.
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    fields: Mapped[list[Field]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    alerts: Mapped[list[Alert]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Field(TimestampMixin, Base):
    """Un campo / lote del productor. Es la unidad geográfica del pronóstico."""

    __tablename__ = "fields"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    # Zona IANA del campo (ej. "America/Argentina/Cordoba"). Define qué es "hoy" para
    # este campo sin importar dónde corra el servidor. Se valida con zoneinfo en la app.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    owner: Mapped[User] = relationship(back_populates="fields")
    forecasts: Mapped[list[WeatherForecast]] = relationship(
        back_populates="field", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(back_populates="field", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )


# --------------------------------------------------------------------------- #
# Tabla que alimenta el job de ingesta (mockeada en este challenge).
# --------------------------------------------------------------------------- #
class WeatherForecast(Base):
    """Una fila = probabilidad de UN evento, para UN campo, en UN día futuro.

    El ingestor hace UPSERT sobre (field_id, forecast_date, event_type), pero sólo pisa
    si el dato entrante es más nuevo:

        INSERT ... ON CONFLICT (field_id, forecast_date, event_type) DO UPDATE
           SET probability = EXCLUDED.probability,
               issued_at   = EXCLUDED.issued_at,
               ingested_at = now()
         WHERE EXCLUDED.issued_at > weather_forecasts.issued_at;

    - Reintento del mismo lote (mismo issued_at) -> no-op: idempotente.
    - Lote viejo que llega tarde -> no-op: no retrocede el pronóstico.
    No guardamos histórico de versiones: el "por qué se avisó" queda en el snapshot
    de alert_notifications.
    """

    __tablename__ = "weather_forecasts"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )  # BIGSERIAL: tabla de alto volumen
    field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_type: Mapped[WeatherEvent] = mapped_column(WEATHER_EVENT_ENUM, nullable=False)
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'mock'"))
    # Cuándo EMITIÓ el proveedor este pronóstico (corrida del modelo). Es lo que mide
    # la frescura del dato; `ingested_at` sólo dice cuándo lo trajimos nosotros.
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    field: Mapped[Field] = relationship(back_populates="forecasts")

    __table_args__ = (
        UniqueConstraint("field_id", "forecast_date", "event_type"),
        CheckConstraint("probability >= 0 AND probability <= 100", name="probability_range"),
        # Acceso del evaluador: "pronósticos de este campo+evento a partir de hoy".
        # El UNIQUE de arriba ya cubre (field_id, forecast_date, ...); este índice
        # sirve el join por (field_id, event_type) con rango de fecha.
        Index("ix_weather_forecasts_field_event_date", "field_id", "event_type", "forecast_date"),
    )


# --------------------------------------------------------------------------- #
# Núcleo del challenge.
# --------------------------------------------------------------------------- #
class Alert(TimestampMixin, Base):
    """Regla configurada por el usuario: "avisame si P(event) >= threshold en este campo"."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # user_id denormalizado (también se deriva de field.owner_id): permite autorizar y
    # listar "mis alertas" sin join, y sostiene el aislamiento multi-tenant.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[WeatherEvent] = mapped_column(WEATHER_EVENT_ENUM, nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        ALERT_STATUS_ENUM, nullable=False, server_default=text("'active'")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic locking para PATCH concurrentes (ETag / If-Match en la API).
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))

    user: Mapped[User] = relationship(back_populates="alerts")
    field: Mapped[Field] = relationship(back_populates="alerts")
    notifications: Mapped[list[AlertNotification]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )
    breach_states: Mapped[list[AlertBreachState]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        # Umbral 0 dispararía siempre: lo excluimos a nivel base.
        CheckConstraint("threshold > 0 AND threshold <= 100", name="threshold_range"),
        # No duplicar la misma regla viva; sí se permiten varios umbrales por evento
        # (ej. lluvia >= 50 "aviso" y >= 80 "crítico").
        Index(
            "uq_alerts_active_rule",
            "field_id",
            "event_type",
            "threshold",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Índice parcial que usa el evaluador: sólo alertas vivas y activas.
        Index(
            "ix_alerts_evaluable",
            "field_id",
            "event_type",
            postgresql_where=text("deleted_at IS NULL AND status = 'active'"),
        ),
        Index("ix_alerts_user_id", "user_id", postgresql_where=text("deleted_at IS NULL")),
    )


class AlertBreachState(Base):
    """Estado de cada (alerta, fecha pronosticada): ¿está ahora por encima del umbral?

    Transiciones (las decide la función pura del dominio, no esta tabla):
        armada  --(prob >= umbral)-->  disparada   => episode += 1
        disparada --(prob >= umbral)-->  disparada => nada (sigue arriba)
        disparada --(prob <  umbral - REARM_MARGIN)-->  armada => se re-arma
        pronóstico stale / faltante              => nada

    Con esto "baja y vuelve a subir" re-avisa siempre, y correr el evaluador N veces con
    el mismo dato no produce transiciones nuevas (idempotente por diseño).
    """

    __tablename__ = "alert_breach_states"

    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), primary_key=True
    )
    forecast_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_breached: Mapped[bool] = mapped_column(nullable=False)
    # Contador de cruces hacia arriba. Es parte de la clave de idempotencia del aviso.
    episode: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    last_probability: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    last_issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    alert: Mapped[Alert] = relationship(back_populates="breach_states")

    __table_args__ = (CheckConstraint("episode >= 0", name="episode_non_negative"),)


class AlertNotification(TimestampMixin, Base):
    """Outbox de notificaciones.

    Una fila nace PENDING cuando el evaluador detecta un umbral superado, y el
    dispatcher la pasa a SENT/FAILED. Separar "detectar" de "enviar" es lo que
    nos da idempotencia + reintentos sin duplicar mensajes.
    """

    __tablename__ = "alert_notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False)
    episode: Mapped[int] = mapped_column(nullable=False)
    # Snapshot: lo que se evaluó en ese momento. Si después cambian el umbral o
    # el pronóstico, el historial sigue contando la verdad de ese instante.
    event_type: Mapped[WeatherEvent] = mapped_column(WEATHER_EVENT_ENUM, nullable=False)
    probability: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        NOTIFICATION_STATUS_ENUM, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    alert: Mapped[Alert] = relationship(back_populates="notifications")

    __table_args__ = (
        # LA garantía de idempotencia: un aviso por cada cruce (episode) de cada alerta
        # y fecha. Si dos evaluadores calculan el mismo cruce a la vez, sólo uno inserta
        # (ON CONFLICT DO NOTHING). El estado y el aviso se escriben en la misma transacción.
        UniqueConstraint("alert_id", "forecast_date", "episode"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # Cola del dispatcher: sólo pendientes, ordenadas por próximo intento.
        Index(
            "ix_alert_notifications_dispatch_queue",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )


class IdempotencyKey(Base):
    """Registro de llaves de idempotencia para llamadas HTTP mutantes (POST /alerts).

    Garantiza que reintentos de red con el mismo Idempotency-Key y mismo body
    devuelvan la respuesta idéntica guardada sin duplicar recursos.
    """

    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    response_headers: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded dict
    response_body: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded body
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_id_key"),)
