"""Initial migration: enums, tables, partial indexes and constraints.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEATHER_EVENT_ENUM = postgresql.ENUM(
    "rain",
    "frost",
    "hail",
    "strong_wind",
    "heat_wave",
    name="weather_event",
    create_type=False,
)
ALERT_STATUS_ENUM = postgresql.ENUM(
    "active",
    "paused",
    name="alert_status",
    create_type=False,
)
NOTIFICATION_STATUS_ENUM = postgresql.ENUM(
    "pending",
    "sent",
    "failed",
    name="notification_status",
    create_type=False,
)


def upgrade() -> None:
    # 1. Enums nativos de PostgreSQL
    WEATHER_EVENT_ENUM.create(op.get_bind(), checkfirst=True)
    ALERT_STATUS_ENUM.create(op.get_bind(), checkfirst=True)
    NOTIFICATION_STATUS_ENUM.create(op.get_bind(), checkfirst=True)

    # 2. users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    # 3. fields
    op.create_table(
        "fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("latitude", sa.Numeric(8, 5), nullable=False),
        sa.Column("longitude", sa.Numeric(8, 5), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_fields_latitude_range"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_fields_longitude_range"),
    )
    op.create_index("ix_fields_owner_id", "fields", ["owner_id"])

    # 4. weather_forecasts
    op.create_table(
        "weather_forecasts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("event_type", WEATHER_EVENT_ENUM, nullable=False),
        sa.Column("probability", sa.Numeric(5, 2), nullable=False),
        sa.Column("source", sa.String(50), server_default=sa.text("'mock'"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "field_id",
            "forecast_date",
            "event_type",
            name="uq_weather_forecasts_field_id_forecast_date_event_type",
        ),
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 100",
            name="ck_weather_forecasts_probability_range",
        ),
    )
    op.create_index(
        "ix_weather_forecasts_field_event_date",
        "weather_forecasts",
        ["field_id", "event_type", "forecast_date"],
    )

    # 5. alerts
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", WEATHER_EVENT_ENUM, nullable=False),
        sa.Column("threshold", sa.Numeric(5, 2), nullable=False),
        sa.Column(
            "status",
            ALERT_STATUS_ENUM,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "threshold > 0 AND threshold <= 100",
            name="ck_alerts_threshold_range",
        ),
    )
    # Índices parciales de alerts
    op.create_index(
        "uq_alerts_active_rule",
        "alerts",
        ["field_id", "event_type", "threshold"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_alerts_evaluable",
        "alerts",
        ["field_id", "event_type"],
        postgresql_where=sa.text("deleted_at IS NULL AND status = 'active'"),
    )
    op.create_index(
        "ix_alerts_user_id",
        "alerts",
        ["user_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # 6. alert_breach_states
    op.create_table(
        "alert_breach_states",
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("forecast_date", sa.Date(), primary_key=True),
        sa.Column("is_breached", sa.Boolean(), nullable=False),
        sa.Column("episode", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_probability", sa.Numeric(5, 2), nullable=False),
        sa.Column("last_issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("episode >= 0", name="ck_alert_breach_states_episode_non_negative"),
    )

    # 7. alert_notifications
    op.create_table(
        "alert_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("event_type", WEATHER_EVENT_ENUM, nullable=False),
        sa.Column("probability", sa.Numeric(5, 2), nullable=False),
        sa.Column("threshold", sa.Numeric(5, 2), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            NOTIFICATION_STATUS_ENUM,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "alert_id",
            "forecast_date",
            "episode",
            name="uq_alert_notifications_alert_id_forecast_date_episode",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_alert_notifications_attempts_non_negative",
        ),
    )
    op.create_index(
        "ix_alert_notifications_dispatch_queue",
        "alert_notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # 8. idempotency_keys
    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_code", sa.SmallInteger(), nullable=False),
        sa.Column("response_headers", sa.Text(), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_id_key"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("alert_notifications")
    op.drop_table("alert_breach_states")
    op.drop_table("alerts")
    op.drop_table("weather_forecasts")
    op.drop_table("fields")
    op.drop_table("users")

    NOTIFICATION_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
    ALERT_STATUS_ENUM.drop(op.get_bind(), checkfirst=True)
    WEATHER_EVENT_ENUM.drop(op.get_bind(), checkfirst=True)
