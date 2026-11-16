"""Bloqueos distribuidos mediante Advisory Locks de PostgreSQL."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agrobot.logging import get_logger

logger = get_logger("db.locks")


class PostgresAdvisoryLock:
    """Implementación de JobLock usando PostgreSQL Advisory Locks.

    Gestiona el ciclo de vida de la conexión: la abre en try_acquire y la cierra
    siempre en release (también si no adquirió el lock).
    """

    def __init__(self, session_or_factory: async_sessionmaker[AsyncSession] | AsyncSession) -> None:
        if isinstance(session_or_factory, AsyncSession):
            self._session_factory: async_sessionmaker[AsyncSession] | None = None
            self._session: AsyncSession | None = session_or_factory
            self._owns_session = False
        else:
            self._session_factory = session_or_factory
            self._session = None
            self._owns_session = True

    async def try_acquire_xact(self, lock_id: int) -> bool:
        """Intenta adquirir el lock a nivel transacción."""
        if self._owns_session and self._session_factory is not None:
            session = self._session_factory()
            try:
                query = select(text("pg_try_advisory_xact_lock(:lock_id)")).params(lock_id=lock_id)
                result = await session.scalar(query)
                acquired = bool(result)
                if not acquired:
                    logger.info("advisory_xact_lock_busy", lock_id=lock_id)
                    await session.close()
                    return False
                self._session = session
                return True
            except Exception:
                await session.close()
                raise
        elif self._session is not None:
            query = select(text("pg_try_advisory_xact_lock(:lock_id)")).params(lock_id=lock_id)
            result = await self._session.scalar(query)
            return bool(result)
        return False

    async def try_acquire(self, lock_id: int) -> bool:
        """Intenta adquirir el lock a nivel sesión."""
        if self._owns_session and self._session_factory is not None:
            session = self._session_factory()
            try:
                query = select(text("pg_try_advisory_lock(:lock_id)")).params(lock_id=lock_id)
                result = await session.scalar(query)
                acquired = bool(result)
                if not acquired:
                    logger.info("advisory_lock_busy", lock_id=lock_id)
                    await session.close()
                    return False
                self._session = session
                return True
            except Exception:
                await session.close()
                raise
        elif self._session is not None:
            query = select(text("pg_try_advisory_lock(:lock_id)")).params(lock_id=lock_id)
            result = await self._session.scalar(query)
            acquired = bool(result)
            if not acquired:
                logger.info("advisory_lock_busy", lock_id=lock_id)
            return acquired
        return False

    async def release(self, lock_id: int) -> None:
        """Libera el lock a nivel sesión y cierra la sesión si es propia."""
        if self._session is not None:
            try:
                query = select(text("pg_advisory_unlock(:lock_id)")).params(lock_id=lock_id)
                await self._session.execute(query)
            finally:
                if self._owns_session:
                    await self._session.close()
                    self._session = None
