"""Global test configuration and fixtures."""

from __future__ import annotations

import os

import pytest

from agrobot.config import settings

DEFAULT_TEST_DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test"


@pytest.fixture(autouse=True, scope="session")
def configure_test_environment() -> None:
    """Ensure tests run against the test database."""
    test_db = os.environ.get("DATABASE_URL", DEFAULT_TEST_DB)
    os.environ["DATABASE_URL"] = test_db
    settings.database_url = test_db
