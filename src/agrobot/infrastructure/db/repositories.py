"""Implementaciones SQLAlchemy de los repositorios de persistencia."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from agrobot.application.pagination import Page, PageMeta, PageParams
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
from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent
from agrobot.domain.errors import AlertNotFound, DuplicateAlert, VersionConflict
from agrobot.infrastructure.db.mappers import (
    to_alert_entity,
    to_alert_model,
    to_breach_state_entity,
    to_field_entity,
    to_forecast_entity,
    to_idempotency_entity,
    to_notification_entity,
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


class SqlAlchemyAlertRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, alert: AlertEntity) -> AlertEntity:
        model = to_alert_model(alert)
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise DuplicateAlert(
                f"Ya existe una alerta activa para el campo {alert.field_id}, "
                f"evento {alert.event_type} y umbral {alert.threshold}"
            ) from exc
        return to_alert_entity(model)

    async def get_by_id(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> AlertEntity | None:
        query = (
            select(AlertModel)
            .options(selectinload(AlertModel.field))
            .where(
                AlertModel.id == alert_id,
                AlertModel.user_id == user_id,
                AlertModel.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(query)
        model = result.scalar_one_or_none()
        return to_alert_entity(model) if model else None

    async def get_by_id_include_deleted(
        self, alert_id: uuid.UUID, user_id: uuid.UUID
    ) -> AlertEntity | None:
        query = (
            select(AlertModel)
            .options(selectinload(AlertModel.field))
            .where(
                AlertModel.id == alert_id,
                AlertModel.user_id == user_id,
            )
        )
        result = await self._session.execute(query)
        model = result.scalar_one_or_none()
        return to_alert_entity(model) if model else None

    async def get_by_id_system(self, alert_id: uuid.UUID) -> AlertEntity | None:
        query = (
            select(AlertModel)
            .options(selectinload(AlertModel.field))
            .where(
                AlertModel.id == alert_id,
                AlertModel.deleted_at.is_(None),
            )
        )
        result = await self._session.execute(query)
        model = result.scalar_one_or_none()
        return to_alert_entity(model) if model else None

    async def find_active_rules(
        self, batch_size: int, after_id: uuid.UUID | None = None
    ) -> list[AlertEntity]:
        query = (
            select(AlertModel)
            .options(selectinload(AlertModel.field))
            .where(
                AlertModel.deleted_at.is_(None),
                AlertModel.status == AlertStatus.ACTIVE,
            )
            .order_by(AlertModel.id.asc())
            .limit(batch_size)
        )
        if after_id is not None:
            query = query.where(AlertModel.id > after_id)

        result = await self._session.execute(query)
        models = list(result.scalars().all())
        return [to_alert_entity(m) for m in models]

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        field_id: uuid.UUID | None = None,
        event_type: WeatherEvent | None = None,
        status: AlertStatus | None = None,
        params: PageParams | None = None,
    ) -> Page[AlertEntity]:
        params = params or PageParams()
        conditions = [AlertModel.user_id == user_id, AlertModel.deleted_at.is_(None)]
        if field_id is not None:
            conditions.append(AlertModel.field_id == field_id)
        if event_type is not None:
            conditions.append(AlertModel.event_type == event_type)
        if status is not None:
            conditions.append(AlertModel.status == status)

        total = (
            await self._session.scalar(
                select(func.count()).select_from(AlertModel).where(*conditions)
            )
            or 0
        )
        result = await self._session.execute(
            select(AlertModel)
            .options(selectinload(AlertModel.field))
            .where(*conditions)
            .order_by(AlertModel.created_at.desc(), AlertModel.id.desc())
            .offset(params.offset)
            .limit(params.page_size)
        )
        items = [to_alert_entity(m) for m in result.scalars().all()]
        return Page(items=items, meta=PageMeta.build(total, params))

    async def update(self, alert: AlertEntity) -> AlertEntity:
        model = await self._session.get(AlertModel, alert.id)
        if model is None:
            raise AlertNotFound(f"Alerta {alert.id} no encontrada")
        model.threshold = alert.threshold
        model.status = alert.status
        model.version = alert.version
        if alert.updated_at is not None:
            model.updated_at = alert.updated_at
        try:
            await self._session.flush()
        except StaleDataError as exc:
            raise VersionConflict("Conflicto de versión concurrente (StaleData)") from exc
        return to_alert_entity(model)

    async def soft_delete(self, alert: AlertEntity) -> None:
        model = await self._session.get(AlertModel, alert.id)
        if model is not None:
            model.deleted_at = alert.deleted_at
            if alert.updated_at is not None:
                model.updated_at = alert.updated_at
            await self._session.flush()

    async def exists_active(
        self,
        field_id: uuid.UUID,
        event_type: WeatherEvent,
        threshold: Decimal,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        query = select(func.count(AlertModel.id)).where(
            AlertModel.field_id == field_id,
            AlertModel.event_type == event_type,
            AlertModel.threshold == threshold,
            AlertModel.deleted_at.is_(None),
        )
        if exclude_id is not None:
            query = query.where(AlertModel.id != exclude_id)
        count = await self._session.scalar(query)
        return (count or 0) > 0


class SqlAlchemyForecastRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, forecast: ForecastEntity) -> bool:
        """Upsert condicional:
        sólo actualiza si EXCLUDED.issued_at > weather_forecasts.issued_at.
        """
        stmt = pg_insert(WeatherForecastModel).values(
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
            where=stmt.excluded.issued_at > WeatherForecastModel.issued_at,
        )
        result = await self._session.execute(upsert_stmt)
        c_res = cast(CursorResult[object], result)
        return bool(c_res.rowcount and c_res.rowcount > 0)

    async def get_for_field(
        self,
        field_id: uuid.UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        event_type: WeatherEvent | None = None,
    ) -> list[ForecastEntity]:
        query = select(WeatherForecastModel).where(WeatherForecastModel.field_id == field_id)
        if from_date is not None:
            query = query.where(WeatherForecastModel.forecast_date >= from_date)
        if to_date is not None:
            query = query.where(WeatherForecastModel.forecast_date <= to_date)
        if event_type is not None:
            query = query.where(WeatherForecastModel.event_type == event_type)

        query = query.order_by(
            WeatherForecastModel.forecast_date.asc(), WeatherForecastModel.event_type.asc()
        )
        result = await self._session.execute(query)
        models = list(result.scalars().all())
        return [to_forecast_entity(m) for m in models]

    async def get_for_alerts(
        self,
        field_ids: list[uuid.UUID],
        event_types: list[WeatherEvent],
        from_date: date,
        to_date: date,
    ) -> list[ForecastEntity]:
        if not field_ids or not event_types:
            return []

        query = select(WeatherForecastModel).where(
            WeatherForecastModel.field_id.in_(field_ids),
            WeatherForecastModel.event_type.in_(event_types),
            WeatherForecastModel.forecast_date >= from_date,
            WeatherForecastModel.forecast_date <= to_date,
        )
        result = await self._session.execute(query)
        models = list(result.scalars().all())
        return [to_forecast_entity(m) for m in models]


class SqlAlchemyBreachStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_states(
        self, alert_ids: list[uuid.UUID], forecast_dates: list[date]
    ) -> dict[tuple[uuid.UUID, date], BreachStateEntity]:
        if not alert_ids or not forecast_dates:
            return {}

        query = select(AlertBreachStateModel).where(
            AlertBreachStateModel.alert_id.in_(alert_ids),
            AlertBreachStateModel.forecast_date.in_(forecast_dates),
        )
        result = await self._session.execute(query)
        states = list(result.scalars().all())
        return {(s.alert_id, s.forecast_date): to_breach_state_entity(s) for s in states}

    async def upsert_state(self, state: BreachStateEntity) -> None:
        stmt = pg_insert(AlertBreachStateModel).values(
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

    async def create_if_not_exists(self, notification: NotificationEntity) -> bool:
        stmt = pg_insert(AlertNotificationModel).values(
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
        c_res = cast(CursorResult[object], result)
        return bool(c_res.rowcount and c_res.rowcount > 0)

    async def get_by_alert(
        self,
        alert_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        params: PageParams | None = None,
    ) -> Page[NotificationEntity]:
        # Bloque 4 Bug 6: No filtrar por deleted_at; historial sigue consultable
        params = params or PageParams()
        conditions = [
            AlertNotificationModel.alert_id == alert_id,
            AlertModel.user_id == user_id,
        ]
        total = (
            await self._session.scalar(
                select(func.count(AlertNotificationModel.id))
                .join(AlertNotificationModel.alert)
                .where(*conditions)
            )
            or 0
        )
        query = (
            select(AlertNotificationModel)
            .join(AlertNotificationModel.alert)
            .where(*conditions)
            .order_by(AlertNotificationModel.created_at.desc(), AlertNotificationModel.id.desc())
            .offset(params.offset)
            .limit(params.page_size)
        )
        result = await self._session.execute(query)
        items = [to_notification_entity(m) for m in result.scalars().all()]
        return Page(items=items, meta=PageMeta.build(total, params))

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: NotificationStatus | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        params: PageParams | None = None,
    ) -> Page[NotificationEntity]:
        # Bloque 4 Bug 6: La bandeja incluye avisos de alertas borradas
        params = params or PageParams()
        conditions = [AlertModel.user_id == user_id]
        if status is not None:
            conditions.append(AlertNotificationModel.status == status)
        if from_date is not None:
            conditions.append(AlertNotificationModel.forecast_date >= from_date)
        if to_date is not None:
            conditions.append(AlertNotificationModel.forecast_date <= to_date)

        total = (
            await self._session.scalar(
                select(func.count(AlertNotificationModel.id))
                .join(AlertNotificationModel.alert)
                .where(*conditions)
            )
            or 0
        )
        query = (
            select(AlertNotificationModel)
            .join(AlertNotificationModel.alert)
            .where(*conditions)
            .order_by(AlertNotificationModel.created_at.desc(), AlertNotificationModel.id.desc())
            .offset(params.offset)
            .limit(params.page_size)
        )
        result = await self._session.execute(query)
        items = [to_notification_entity(m) for m in result.scalars().all()]
        return Page(items=items, meta=PageMeta.build(total, params))

    async def claim_pending(
        self, limit: int, lease_duration: timedelta, now: datetime
    ) -> list[NotificationEntity]:
        """Reclama hasta `limit` notificaciones pendientes usando FOR UPDATE SKIP LOCKED.

        Extiende `next_attempt_at = now + lease_duration` e incrementa `attempts`.
        """
        query = (
            select(AlertNotificationModel)
            .options(selectinload(AlertNotificationModel.alert).selectinload(AlertModel.user))
            .where(
                AlertNotificationModel.status == NotificationStatus.PENDING,
                AlertNotificationModel.next_attempt_at <= now,
            )
            .order_by(AlertNotificationModel.next_attempt_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(query)
        claimed = list(result.scalars().all())

        entities: list[NotificationEntity] = []
        for notification in claimed:
            notification.attempts += 1
            notification.next_attempt_at = now + lease_duration
            ent = to_notification_entity(notification)
            ent.attempts = notification.attempts
            ent.next_attempt_at = notification.next_attempt_at
            entities.append(ent)

        await self._session.flush()
        return entities

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
        query = select(AlertNotificationModel).where(AlertNotificationModel.id == notification_id)
        result = await self._session.execute(query)
        notif = result.scalar_one_or_none()
        if notif:
            notif.status = status
            notif.attempts = attempts
            if next_attempt_at is not None:
                notif.next_attempt_at = next_attempt_at
            notif.last_error = last_error
            notif.sent_at = sent_at
            if notif.created_at:
                notif.updated_at = datetime.now(notif.created_at.tzinfo)
            else:
                notif.updated_at = func.now()
            await self._session.flush()

    async def mark_alert_notifications_failed(self, alert_id: uuid.UUID, last_error: str) -> int:
        stmt = (
            update(AlertNotificationModel)
            .where(
                AlertNotificationModel.alert_id == alert_id,
                AlertNotificationModel.status == NotificationStatus.PENDING,
            )
            .values(
                status=NotificationStatus.FAILED,
                last_error=last_error,
                updated_at=func.now(),
            )
        )
        result = await self._session.execute(stmt)
        c_res = cast(CursorResult[object], result)
        return int(c_res.rowcount or 0)


class SqlAlchemyFieldRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, field_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> FieldEntity | None:
        query = select(FieldModel).where(FieldModel.id == field_id)
        if user_id is not None:
            query = query.where(FieldModel.owner_id == user_id)
        result = await self._session.execute(query)
        model = result.scalar_one_or_none()
        return to_field_entity(model) if model else None


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID, key: str) -> IdempotencyEntity | None:
        query = select(IdempotencyKeyModel).where(
            IdempotencyKeyModel.user_id == user_id,
            IdempotencyKeyModel.key == key,
            IdempotencyKeyModel.expires_at > func.now(),
        )
        result = await self._session.execute(query)
        model = result.scalar_one_or_none()
        return to_idempotency_entity(model) if model else None

    async def save(self, record: IdempotencyEntity) -> None:
        stmt = pg_insert(IdempotencyKeyModel).values(
            id=record.id,
            user_id=record.user_id,
            key=record.key,
            request_hash=record.request_hash,
            response_code=record.response_code,
            response_headers=record.response_headers,
            response_body=record.response_body,
            created_at=record.created_at,
            expires_at=record.expires_at,
        )
        upsert_stmt = stmt.on_conflict_do_update(
            constraint="uq_idempotency_keys_user_id_key",
            set_={
                "request_hash": stmt.excluded.request_hash,
                "response_code": stmt.excluded.response_code,
                "response_headers": stmt.excluded.response_headers,
                "response_body": stmt.excluded.response_body,
                "created_at": stmt.excluded.created_at,
                "expires_at": stmt.excluded.expires_at,
            },
            where=IdempotencyKeyModel.expires_at <= func.now(),
        )
        await self._session.execute(upsert_stmt)
        await self._session.flush()
