"""Tests unitarios para el shutdown ordenado del worker."""

from __future__ import annotations

import asyncio

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agrobot.worker.scheduler import graceful_shutdown


@pytest.mark.asyncio
async def test_slow_job_completes_without_cancellation_during_graceful_shutdown() -> None:
    """Verifica que un job lento en vuelo termina con éxito durante el shutdown
    sin CancelledError.
    """
    scheduler = AsyncIOScheduler()
    scheduler.start()

    inflight_tasks: set[asyncio.Task[None]] = set()
    job_finished = False

    async def slow_job() -> None:
        nonlocal job_finished
        task = asyncio.current_task()
        assert task is not None
        inflight_tasks.add(task)
        try:
            await asyncio.sleep(0.1)
            job_finished = True
        finally:
            inflight_tasks.discard(task)

    # Iniciar la tarea en segundo plano
    running_task = asyncio.create_task(slow_job())
    await asyncio.sleep(0.01)  # Asegurar que arrancó y está en inflight_tasks
    assert running_task in inflight_tasks

    # Ejecutar shutdown con suficiente tiempo de espera
    await graceful_shutdown(scheduler, inflight_tasks, timeout_s=1.0)

    # La tarea debe haber terminado normalmente, sin CancelledError
    assert job_finished is True
    assert running_task.done()
    assert not running_task.cancelled()


@pytest.mark.asyncio
async def test_job_exceeding_timeout_is_cancelled() -> None:
    """Verifica que una tarea que excede el timeout de shutdown es cancelada."""
    scheduler = AsyncIOScheduler()
    scheduler.start()

    inflight_tasks: set[asyncio.Task[None]] = set()

    async def very_slow_job() -> None:
        task = asyncio.current_task()
        assert task is not None
        inflight_tasks.add(task)
        try:
            await asyncio.sleep(5.0)
        finally:
            inflight_tasks.discard(task)

    running_task = asyncio.create_task(very_slow_job())
    await asyncio.sleep(0.01)
    assert running_task in inflight_tasks

    # Shutdown con timeout muy corto (0.05s)
    await graceful_shutdown(scheduler, inflight_tasks, timeout_s=0.05)

    with pytest.raises(asyncio.CancelledError):
        await running_task

    assert running_task.cancelled()
