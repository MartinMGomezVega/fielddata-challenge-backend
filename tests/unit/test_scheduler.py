"""Tests unitarios para la configuración del scheduler en APScheduler."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from apscheduler.triggers.interval import IntervalTrigger

from agrobot.infrastructure.clock import FrozenClock
from agrobot.worker.scheduler import create_scheduler


def test_scheduler_registered_jobs_configuration() -> None:
    uow_factory = MagicMock()
    clock = FrozenClock(datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    sender = MagicMock()
    lock_factory = MagicMock()

    scheduler = create_scheduler(
        uow_factory=uow_factory,
        clock=clock,
        sender=sender,
        job_lock_factory=lock_factory,
        eval_interval_min=15,
        dispatch_interval_s=30,
        eval_jitter=15,
    )

    jobs = scheduler.get_jobs()
    job_map = {job.id: job for job in jobs}

    # 1. Ambos jobs deben estar registrados
    assert "evaluate_alerts" in job_map
    assert "dispatch_notifications" in job_map
    assert len(jobs) == 2

    # 2. Configuración de EvaluateAlerts
    eval_job = job_map["evaluate_alerts"]
    assert eval_job.max_instances == 1
    assert eval_job.coalesce is True
    assert eval_job.misfire_grace_time == 60
    assert isinstance(eval_job.trigger, IntervalTrigger)
    assert eval_job.trigger.interval.total_seconds() == 15 * 60
    assert eval_job.trigger.jitter == 15

    # 3. Configuración de DispatchNotifications
    dispatch_job = job_map["dispatch_notifications"]
    assert dispatch_job.max_instances == 1
    assert dispatch_job.coalesce is True
    assert isinstance(dispatch_job.trigger, IntervalTrigger)
    assert dispatch_job.trigger.interval.total_seconds() == 30
