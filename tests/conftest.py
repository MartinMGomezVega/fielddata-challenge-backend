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
    """Ensure tests run against the test database."""
    test_db = os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = test_db
    settings.database_url = test_db


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    test_db = os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)
    engine = create_async_engine(test_db, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)
