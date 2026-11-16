"""Test de integración para verificar la ausencia de connection leaks en PostgresAdvisoryLock."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agrobot.infrastructure.db.locks import PostgresAdvisoryLock


@pytest.mark.asyncio
async def test_advisory_lock_no_idle_in_transaction_leak(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verifica que tras múltiples adquisiciones y liberaciones del lock,

    no queden conexiones abiertas en 'idle in transaction'.
    """
    lock_id = 999123

    # Ejecutar 5 corridas normales de acquire y release
    for _ in range(5):
        lock = PostgresAdvisoryLock(session_factory)
        acquired = await lock.try_acquire(lock_id)
        assert acquired is True
        await lock.release(lock_id)

    # Simular intento cuando el lock está ocupado por otra sesión
    async with session_factory() as holding_session:
        # Adquirir el lock en la sesión de control
        await holding_session.execute(
            text("SELECT pg_advisory_lock(:lock_id)").params(lock_id=lock_id)
        )

        # 5 intentos de PostgresAdvisoryLock que deben fallar y cerrar de inmediato su sesión
        for _ in range(5):
            busy_lock = PostgresAdvisoryLock(session_factory)
            acquired = await busy_lock.try_acquire(lock_id)
            assert acquired is False

        # Liberar el lock de control
        await holding_session.execute(
            text("SELECT pg_advisory_unlock(:lock_id)").params(lock_id=lock_id)
        )
        await holding_session.commit()

    # Verificar en pg_stat_activity que no haya conexiones colgadas en 'idle in transaction'
    async with session_factory() as verify_session:
        result = await verify_session.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND state = 'idle in transaction'"
            )
        )
        idle_in_tx_count = result.scalar()
        assert idle_in_tx_count == 0, (
            f"Se encontraron {idle_in_tx_count} conexiones en 'idle in transaction' en la base"
        )
