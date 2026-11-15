"""Implementación de UnitOfWork con SQLAlchemy AsyncSession."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agrobot.application.ports import (
    AlertRepository,
    BreachStateRepository,
    FieldRepository,
    ForecastRepository,
    IdempotencyRepository,
    NotificationRepository,
    UnitOfWork,
)
from agrobot.infrastructure.db.repositories import (
    SqlAlchemyAlertRepository,
    SqlAlchemyBreachStateRepository,
    SqlAlchemyFieldRepository,
    SqlAlchemyForecastRepository,
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyNotificationRepository,
)
from agrobot.infrastructure.db.session import get_session_factory


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Maneja el ciclo de vida de la transacción en una AsyncSession."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.alerts: AlertRepository = SqlAlchemyAlertRepository(self._session)
        self.forecasts: ForecastRepository = SqlAlchemyForecastRepository(self._session)
        self.breach_states: BreachStateRepository = SqlAlchemyBreachStateRepository(self._session)
        self.notifications: NotificationRepository = SqlAlchemyNotificationRepository(self._session)
        self.fields: FieldRepository = SqlAlchemyFieldRepository(self._session)
        self.idempotency: IdempotencyRepository = SqlAlchemyIdempotencyRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        if self._session is not None:
            if exc_type is not None:
                await self.rollback()
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        if self._session is not None:
            await self._session.commit()

    async def rollback(self) -> None:
        if self._session is not None:
            await self._session.rollback()

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork fuera de contexto activo.")
        return self._session
