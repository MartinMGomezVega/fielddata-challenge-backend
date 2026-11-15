"""Implementaciones SQLAlchemy de los repositorios de persistencia."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agrobot.application.use_cases.manage_alerts import decode_cursor, encode_cursor
from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent
from agrobot.infrastructure.db.models import (
    Alert,
    AlertBreachState,
    AlertNotification,
    Field,
    IdempotencyKey,
    WeatherForecast,
)


class SqlAlchemyAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, alert: Alert) -> Alert:
        self._session.add(alert)
        await self._session.flush()
        return alert

    async def get_by_id(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> Alert | None:
        query = select(Alert).where(
            Alert.id == alert_id,
            Alert.user_id == user_id,
            Alert.deleted_at.is_(None),
        )
        result = await self._session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id_system(self, alert_id: uuid.UUID) -> Alert | None:
        query = select(Alert).where(
            Alert.id == alert_id,
            Alert.deleted_at.is_(None),
        )
        result = await self._session.execute(query)
        return result.scalar_one_or_none()

    async def find_active_rules(
        self, batch_size: int, after_id: uuid.UUID | None = None
    ) -> list[Alert]:
        query = (
            select(Alert)
            .options(selectinload(Alert.field))
            .where(
                Alert.deleted_at.is_(None),
                Alert.status == AlertStatus.ACTIVE,
            )
            .order_by(Alert.id.asc())
            .limit(batch_size)
        )
        if after_id is not None:
            query = query.where(Alert.id > after_id)

        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        field_id: uuid.UUID | None = None,
        event_type: WeatherEvent | None = None,
        status: AlertStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Alert], str | None]:
        query = select(Alert).where(
            Alert.user_id == user_id,
            Alert.deleted_at.is_(None),
        )
        if field_id is not None:
            query = query.where(Alert.field_id == field_id)
        if event_type is not None:
            query = query.where(Alert.event_type == event_type)
        if status is not None:
            query = query.where(Alert.status == status)

        if cursor is not None:
            decoded = decode_cursor(cursor)
            if decoded is not None:
                c_dt, c_id = decoded
                query = query.where(tuple_(Alert.created_at, Alert.id) < tuple_(c_dt, c_id))

        query = query.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit + 1)
        result = await self._session.execute(query)
        items = list(result.scalars().all())

        next_cursor = None
        if len(items) > limit:
            items = items[:limit]
            last_item = items[-1]
            next_cursor = encode_cursor(last_item.created_at, last_item.id)

        return items, next_cursor

    async def update(self, alert: Alert) -> Alert:
        merged = await self._session.merge(alert)
        await self._session.flush()
        return merged

    async def soft_delete(self, alert: Alert) -> None:
        await self._session.merge(alert)
        await self._session.flush()

    async def exists_active(
        self,
        field_id: uuid.UUID,
        event_type: WeatherEvent,
        threshold: Decimal,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        query = select(func.count(Alert.id)).where(
            Alert.field_id == field_id,
            Alert.event_type == event_type,
            Alert.threshold == threshold,
            Alert.deleted_at.is_(None),
        )
        if exclude_id is not None:
            query = query.where(Alert.id != exclude_id)
        count = await self._session.scalar(query)
        return (count or 0) > 0


class SqlAlchemyForecastRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, forecast: WeatherForecast) -> bool:
        """Upsert condicional:
        sólo actualiza si EXCLUDED.issued_at > weather_forecasts.issued_at.
        """
        stmt = pg_insert(WeatherForecast).values(
            field_id=forecast.field_id,
            forecast_date=forecast.forecast_date,
            event_type=forecast.event_type,
            probability=forecast.probability,
            source=forecast.source or "mock",
            issued_at=forecast.issued_at,
            ingested_at=func.now(),
        )
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["field_id", "forecast_date", "event_type"],
            set_={
                "probability": stmt.excluded.probability,
                "issued_at": stmt.excluded.issued_at,
                "source": stmt.excluded.source,
                "ingested_at": func.now(),
            },
            where=stmt.excluded.issued_at > WeatherForecast.issued_at,
        )
        result = await self._session.execute(upsert_stmt)
        cursor_result = cast(CursorResult[object], result)
        return bool(cursor_result.rowcount and cursor_result.rowcount > 0)

    async def get_for_field(
        self,
        field_id: uuid.UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        event_type: WeatherEvent | None = None,
    ) -> list[WeatherForecast]:
        query = select(WeatherForecast).where(WeatherForecast.field_id == field_id)
        if from_date is not None:
            query = query.where(WeatherForecast.forecast_date >= from_date)
        if to_date is not None:
            query = query.where(WeatherForecast.forecast_date <= to_date)
        if event_type is not None:
            query = query.where(WeatherForecast.event_type == event_type)

        query = query.order_by(
            WeatherForecast.forecast_date.asc(), WeatherForecast.event_type.asc()
        )
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def get_for_alerts(
        self,
        field_ids: list[uuid.UUID],
        event_types: list[WeatherEvent],
        from_date: date,
        to_date: date,
    ) -> list[WeatherForecast]:
        if not field_ids or not event_types:
            return []

        query = select(WeatherForecast).where(
            WeatherForecast.field_id.in_(field_ids),
            WeatherForecast.event_type.in_(event_types),
            WeatherForecast.forecast_date >= from_date,
            WeatherForecast.forecast_date <= to_date,
        )
        result = await self._session.execute(query)
        return list(result.scalars().all())


class SqlAlchemyBreachStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_states(
        self, alert_ids: list[uuid.UUID], forecast_dates: list[date]
    ) -> dict[tuple[uuid.UUID, date], AlertBreachState]:
        if not alert_ids or not forecast_dates:
            return {}

        query = select(AlertBreachState).where(
            AlertBreachState.alert_id.in_(alert_ids),
            AlertBreachState.forecast_date.in_(forecast_dates),
        )
        result = await self._session.execute(query)
        states = list(result.scalars().all())
        return {(s.alert_id, s.forecast_date): s for s in states}

    async def upsert_state(self, state: AlertBreachState) -> None:
        stmt = pg_insert(AlertBreachState).values(
            alert_id=state.alert_id,
            forecast_date=state.forecast_date,
            is_breached=state.is_breached,
            episode=state.episode,
            last_probability=state.last_probability,
            last_issued_at=state.last_issued_at,
            updated_at=func.now(),
        )
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["alert_id", "forecast_date"],
            set_={
                "is_breached": stmt.excluded.is_breached,
                "episode": stmt.excluded.episode,
                "last_probability": stmt.excluded.last_probability,
                "last_issued_at": stmt.excluded.last_issued_at,
                "updated_at": func.now(),
            },
        )
        await self._session.execute(upsert_stmt)


class SqlAlchemyNotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_if_not_exists(self, notification: AlertNotification) -> bool:
        stmt = pg_insert(AlertNotification).values(
            id=notification.id,
            alert_id=notification.alert_id,
            forecast_date=notification.forecast_date,
            episode=notification.episode,
            event_type=notification.event_type,
            probability=notification.probability,
            threshold=notification.threshold,
            issued_at=notification.issued_at,
            status=notification.status or NotificationStatus.PENDING,
            attempts=notification.attempts if notification.attempts is not None else 0,
            next_attempt_at=notification.next_attempt_at or func.now(),
            last_error=notification.last_error,
            sent_at=notification.sent_at,
            created_at=func.now(),
            updated_at=func.now(),
        )
        insert_stmt = stmt.on_conflict_do_nothing(
            index_elements=["alert_id", "forecast_date", "episode"]
        )
        result = await self._session.execute(insert_stmt)
        cursor_result = cast(CursorResult[object], result)
        return bool(cursor_result.rowcount and cursor_result.rowcount > 0)

    async def get_by_alert(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[AlertNotification], str | None]:
        query = (
            select(AlertNotification)
            .join(AlertNotification.alert)
            .where(
                AlertNotification.alert_id == alert_id,
                Alert.user_id == user_id,
                Alert.deleted_at.is_(None),
            )
        )
        if cursor is not None:
            decoded = decode_cursor(cursor)
            if decoded is not None:
                c_dt, c_id = decoded
                query = query.where(
                    tuple_(AlertNotification.created_at, AlertNotification.id) < tuple_(c_dt, c_id)
                )

        query = query.order_by(
            AlertNotification.created_at.desc(), AlertNotification.id.desc()
        ).limit(limit + 1)
        result = await self._session.execute(query)
        items = list(result.scalars().all())

        next_cursor = None
        if len(items) > limit:
            items = items[:limit]
            last_item = items[-1]
            next_cursor = encode_cursor(last_item.created_at, last_item.id)

        return items, next_cursor

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: NotificationStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[AlertNotification], str | None]:
        query = (
            select(AlertNotification)
            .join(AlertNotification.alert)
            .where(
                Alert.user_id == user_id,
                Alert.deleted_at.is_(None),
            )
        )
        if status is not None:
            query = query.where(AlertNotification.status == status)
        if from_date is not None:
            query = query.where(AlertNotification.forecast_date >= from_date)
        if to_date is not None:
            query = query.where(AlertNotification.forecast_date <= to_date)

        if cursor is not None:
            decoded = decode_cursor(cursor)
            if decoded is not None:
                c_dt, c_id = decoded
                query = query.where(
                    tuple_(AlertNotification.created_at, AlertNotification.id) < tuple_(c_dt, c_id)
                )

        query = query.order_by(
            AlertNotification.created_at.desc(), AlertNotification.id.desc()
        ).limit(limit + 1)
        result = await self._session.execute(query)
        items = list(result.scalars().all())

        next_cursor = None
        if len(items) > limit:
            items = items[:limit]
            last_item = items[-1]
            next_cursor = encode_cursor(last_item.created_at, last_item.id)

        return items, next_cursor

    async def claim_pending(self, limit: int, lease_duration: timedelta) -> list[AlertNotification]:
        """Reclama hasta `limit` notificaciones pendientes usando FOR UPDATE SKIP LOCKED.

        Extiende `next_attempt_at = now() + lease_duration` e incrementa `attempts`.
        """
        now = func.now()
        query = (
            select(AlertNotification)
            .options(selectinload(AlertNotification.alert).selectinload(Alert.user))
            .where(
                AlertNotification.status == NotificationStatus.PENDING,
                AlertNotification.next_attempt_at <= now,
            )
            .order_by(AlertNotification.next_attempt_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(query)
        claimed = list(result.scalars().all())

        for notification in claimed:
            notification.attempts += 1
            notification.next_attempt_at = datetime.now(UTC) + lease_duration

        await self._session.flush()
        return claimed

    async def update_status(
        self,
        notification_id: uuid.UUID,
        status: NotificationStatus,
        *,
        attempts: int,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
        sent_at: datetime | None = None,
    ) -> None:
        query = select(AlertNotification).where(AlertNotification.id == notification_id)
        result = await self._session.execute(query)
        notif = result.scalar_one_or_none()
        if notif:
            notif.status = status
            notif.attempts = attempts
            if next_attempt_at is not None:
                notif.next_attempt_at = next_attempt_at
            notif.last_error = last_error
            notif.sent_at = sent_at
            notif.updated_at = datetime.now(UTC)
            await self._session.flush()


class SqlAlchemyFieldRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, field_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> Field | None:
        query = select(Field).where(Field.id == field_id)
        if user_id is not None:
            query = query.where(Field.owner_id == user_id)
        result = await self._session.execute(query)
        return result.scalar_one_or_none()


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, key: str) -> IdempotencyKey | None:
        query = select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at > func.now(),
        )
        result = await self._session.execute(query)
        return result.scalar_one_or_none()

    async def save(self, record: IdempotencyKey) -> None:
        self._session.add(record)
        await self._session.flush()
