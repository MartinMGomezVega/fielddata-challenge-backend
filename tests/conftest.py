"""Global test configuration and fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agrobot.config import settings

DEFAULT_TEST_DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test"


@pytest.fixture(autouse=True, scope="session")
def configure_test_environment() -> None:
    """Ensure tests run against the test database and tables exist."""
    test_db = os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = test_db
    settings.database_url = test_db

    import asyncio
    from urllib.parse import urlparse

    import asyncpg
    from alembic import command
    from alembic.config import Config

    parsed = urlparse(test_db.replace("+asyncpg", ""))
    db_name = parsed.path.lstrip("/")
    admin_url = (
        f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port}/postgres"
    )

    async def _init_test_db() -> None:
        try:
            conn = await asyncpg.connect(admin_url)
            try:
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1", db_name
                )
                if not exists:
                    await conn.execute(f'CREATE DATABASE "{db_name}"')
            finally:
                await conn.close()
        except Exception:
            pass

    try:
        asyncio.run(_init_test_db())
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", test_db)
        command.upgrade(alembic_cfg, "head")
    except Exception:
        pass


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    test_db = os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)
    engine = create_async_engine(test_db, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)
