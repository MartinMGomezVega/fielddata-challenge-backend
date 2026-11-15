"""Bloqueos distribuidos mediante Advisory Locks de PostgreSQL."""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agrobot.logging import get_logger

logger = get_logger("db.locks")


class PostgresAdvisoryLock:
    """Implementación de JobLock usando PostgreSQL Advisory Locks.

    Usa pg_try_advisory_xact_lock a nivel transacción (se libera automáticamente
    al terminar la transacción) o pg_try_advisory_lock a nivel de sesión.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_acquire_xact(self, lock_id: int) -> bool:
        """Intenta adquirir el lock a nivel transacción.

        Se libera automáticamente en commit o rollback.
        """
        query = select(text("pg_try_advisory_xact_lock(:lock_id)")).params(lock_id=lock_id)
        result = await self._session.scalar(query)
        acquired = bool(result)
        if not acquired:
            logger.info("advisory_xact_lock_busy", lock_id=lock_id)
        return acquired

    async def try_acquire(self, lock_id: int) -> bool:
        """Intenta adquirir el lock a nivel sesión."""
        query = select(text("pg_try_advisory_lock(:lock_id)")).params(lock_id=lock_id)
        result = await self._session.scalar(query)
        acquired = bool(result)
        if not acquired:
            logger.info("advisory_lock_busy", lock_id=lock_id)
        return acquired

    async def release(self, lock_id: int) -> None:
        """Libera el lock a nivel sesión."""
        query = select(text("pg_advisory_unlock(:lock_id)")).params(lock_id=lock_id)
        await self._session.execute(query)
