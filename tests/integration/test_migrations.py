"""Test de migraciones Alembic: upgrade head -> downgrade base -> upgrade head."""

import os

from alembic import command
from alembic.config import Config


def test_alembic_migrations_lifecycle() -> None:
    alembic_cfg = Config("alembic.ini")
    db_url = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test"
    )
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. upgrade head
    command.upgrade(alembic_cfg, "head")

    # 2. downgrade base
    command.downgrade(alembic_cfg, "base")

    # 3. upgrade head
    command.upgrade(alembic_cfg, "head")
