"""Punto de entrada del proceso Worker (python -m agrobot.worker.main).

Maneja señales del sistema operativo (SIGTERM, SIGINT) para un apagado ordenado
sin cortar transacciones en curso (scheduler.shutdown(wait=True) y dispose_engine).
"""

from __future__ import annotations

import asyncio
import signal

from agrobot.config import settings
from agrobot.infrastructure.clock import SystemClock
from agrobot.infrastructure.db.locks import PostgresAdvisoryLock
from agrobot.infrastructure.db.session import dispose_engine, get_engine, get_session_factory
from agrobot.infrastructure.db.uow import SqlAlchemyUnitOfWork
from agrobot.infrastructure.notifications.logging_sender import LoggingSender
from agrobot.logging import configure_logging, get_logger
from agrobot.worker.scheduler import create_scheduler, graceful_shutdown

logger = get_logger("worker.main")


async def main() -> None:
    configure_logging(settings.log_level)
    logger.info(
        "worker_starting",
        environment=settings.environment,
        eval_interval_min=settings.eval_interval_min,
        dispatch_interval_s=settings.dispatch_interval_s,
    )

    engine = get_engine()
    session_factory = get_session_factory(engine)
    clock = SystemClock()
    sender = LoggingSender()

    # Cada job abre su propia sesión (nunca compartir sesiones entre tareas)
    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    def job_lock_factory() -> PostgresAdvisoryLock:
        return PostgresAdvisoryLock(session_factory)

    inflight_tasks: set[asyncio.Task[None]] = set()

    scheduler = create_scheduler(
        uow_factory=uow_factory,
        clock=clock,
        sender=sender,
        job_lock_factory=job_lock_factory,
        inflight_tasks=inflight_tasks,
    )

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    scheduler.start()
    logger.info("worker_scheduler_started")

    try:
        await shutdown_event.wait()
        logger.info("worker_shutdown_signal_received")
    finally:
        await graceful_shutdown(
            scheduler=scheduler,
            inflight_tasks=inflight_tasks,
            timeout_s=settings.shutdown_timeout_s,
        )
        logger.info("worker_disposing_db_engine")
        await dispose_engine()
        logger.info("worker_shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
