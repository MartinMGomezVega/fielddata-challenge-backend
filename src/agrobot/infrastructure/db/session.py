"""Configuración del Engine y Session Factory async de SQLAlchemy."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agrobot.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(database_url: str | None = None) -> AsyncEngine:
    """Retorna o inicializa el motor async de SQLAlchemy (singleton)."""
    global _engine
    if _engine is None:
        url = database_url or settings.database_url
        _engine = create_async_engine(
            url,
            echo=False,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Retorna o inicializa el creador de sesiones asincrónicas."""
    global _session_factory
    if _session_factory is None or engine is not None:
        target_engine = engine or get_engine()
        _session_factory = async_sessionmaker(
            bind=target_engine,
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones para inyección de dependencias en FastAPI."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    """Cierra las conexiones del pool al apagar la aplicación."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
