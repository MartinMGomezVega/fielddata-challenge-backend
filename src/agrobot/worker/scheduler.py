"""Configuración y registro de jobs en APScheduler 3.x AsyncIOScheduler."""

from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from agrobot.application.ports import Clock, JobLock, NotificationSender, UnitOfWork
from agrobot.application.use_cases.dispatch_notifications import DispatchNotifications
from agrobot.application.use_cases.evaluate_alerts import EvaluateAlerts
from agrobot.config import settings
from agrobot.logging import get_logger

logger = get_logger("worker.scheduler")


def create_scheduler(
    uow_factory: Callable[[], UnitOfWork],
    clock: Clock,
    sender: NotificationSender,
    job_lock_factory: Callable[[], JobLock],
    eval_interval_min: int | None = None,
    dispatch_interval_s: int | None = None,
    eval_jitter: int = 30,
) -> AsyncIOScheduler:
    """Crea y configura el scheduler asincrónico con los dos background jobs."""
    scheduler = AsyncIOScheduler()

    eval_interval = eval_interval_min or settings.eval_interval_min
    dispatch_interval = dispatch_interval_s or settings.dispatch_interval_s

    async def evaluate_job() -> None:
        """Job periódico de evaluación: abre una sesión nueva exclusiva para esta corrida."""
        uow = uow_factory()
        lock = job_lock_factory()
        evaluator = EvaluateAlerts(
            uow=uow,
            clock=clock,
            job_lock=lock,
            batch_size=settings.batch_size,
            horizon_days=settings.horizon_days,
            max_age_hours=settings.max_forecast_age_hours,
            rearm_margin=settings.rearm_margin,
        )
        try:
            await evaluator.run()
        except Exception as exc:
            logger.error("evaluate_alerts_job_unhandled_exception", error=str(exc))

    async def dispatch_job() -> None:
        """Job periódico de despacho de notificaciones: lease pattern fuera de tx."""
        dispatcher = DispatchNotifications(
            uow=uow_factory,
            sender=sender,
            clock=clock,
            batch_size=50,
            lease_duration_s=settings.lease_duration_s,
            max_attempts=settings.max_attempts,
            concurrency=settings.dispatch_concurrency,
        )
        try:
            await dispatcher.run()
        except Exception as exc:
            logger.error("dispatch_notifications_job_unhandled_exception", error=str(exc))

    # Job 1: EvaluateAlerts (cada EVAL_INTERVAL_MIN con jitter)
    scheduler.add_job(
        evaluate_job,
        trigger=IntervalTrigger(minutes=eval_interval, jitter=eval_jitter),
        id="evaluate_alerts",
        name="Evaluate climate alerts against fresh forecasts",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )

    # Job 2: DispatchNotifications (cada DISPATCH_INTERVAL_S)
    scheduler.add_job(
        dispatch_job,
        trigger=IntervalTrigger(seconds=dispatch_interval),
        id="dispatch_notifications",
        name="Claim and dispatch pending notifications from outbox",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    return scheduler
