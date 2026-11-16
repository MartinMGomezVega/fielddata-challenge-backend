"""Caso de uso: EvaluateAlerts (Job en Background).

Recorre alertas activas por lotes, consulta pronósticos y estados previos en bulk,
ejecuta la máquina de estados pura y persiste estados y notificaciones atómicamente.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from agrobot.application.ports import Clock, JobLock, UnitOfWork
from agrobot.config import settings
from agrobot.domain.entities import (
    AlertRule,
    BreachState,
    Fire,
    Forecast,
    ForecastPoint,
    Hold,
    Notification,
    Rearm,
    Skip,
)
from agrobot.domain.enums import NotificationStatus
from agrobot.domain.evaluator import evaluate
from agrobot.logging import get_logger

logger = get_logger("jobs.evaluate_alerts")

EVALUATE_ALERTS_LOCK_ID = 884210


@dataclass(frozen=True)
class RunReport:
    """Reporte estructurado con métricas de la corrida de evaluación."""

    run_id: str
    alerts_evaluated: int
    fired: int
    held: int
    rearmed: int
    stale: int
    out_of_window: int
    duplicated: int
    duration_seconds: float
    skipped: bool = False
    reason: str | None = None


class EvaluateAlerts:
    """Orquestador del job periódico de evaluación de alertas."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        job_lock: JobLock,
        batch_size: int | None = None,
        horizon_days: int | None = None,
        max_age_hours: int | None = None,
        rearm_margin: Decimal | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._lock = job_lock
        self._batch_size = batch_size or settings.batch_size
        self._horizon_days = horizon_days or settings.horizon_days
        self._max_age_hours = max_age_hours or settings.max_forecast_age_hours
        self._rearm_margin = rearm_margin if rearm_margin is not None else settings.rearm_margin

    async def run(self) -> RunReport:
        run_id = str(uuid.uuid4())
        start_time = time.monotonic()

        # 1. Intentar adquirir lock de exclusión mutua para réplicas concurrentes
        if self._lock is not None:
            acquired = await self._lock.try_acquire(EVALUATE_ALERTS_LOCK_ID)
            if not acquired:
                logger.info("evaluate_alerts_skipped_concurrent_execution", run_id=run_id)
                return RunReport(
                    run_id=run_id,
                    alerts_evaluated=0,
                    fired=0,
                    held=0,
                    rearmed=0,
                    stale=0,
                    out_of_window=0,
                    duplicated=0,
                    duration_seconds=0.0,
                    skipped=True,
                    reason="Advisory lock busy (another replica running)",
                )

        logger.info("evaluate_alerts_started", run_id=run_id)
        alerts_evaluated = 0
        fired_count = 0
        held_count = 0
        rearmed_count = 0
        stale_count = 0
        out_of_window_count = 0
        duplicated_count = 0

        try:
            now_utc = self._clock.now_utc()
            max_age = timedelta(hours=self._max_age_hours)
            after_id: uuid.UUID | None = None

            while True:
                async with self._uow as uow:
                    batch = await uow.alerts.find_active_rules(
                        batch_size=self._batch_size, after_id=after_id
                    )
                    if not batch:
                        break

                    alerts_evaluated += len(batch)
                    after_id = batch[-1].id

                    # Agrupar campos y eventos
                    field_ids = list({a.field_id for a in batch})
                    event_types = list({a.event_type for a in batch})

                    # Calcular ventana global mínima y máxima del lote
                    min_date = min(
                        now_utc.astimezone(ZoneInfo(a.field.timezone)).date()
                        for a in batch
                        if a.field is not None
                    )
                    max_date = max(
                        now_utc.astimezone(ZoneInfo(a.field.timezone)).date()
                        + timedelta(days=self._horizon_days)
                        for a in batch
                        if a.field is not None
                    )

                    # Consulta en bulk de pronósticos
                    forecasts = await uow.forecasts.get_for_alerts(
                        field_ids, event_types, min_date, max_date
                    )
                    forecast_map: dict[tuple[uuid.UUID, str], list[Forecast]] = {}
                    forecast_dates: set[date] = set()
                    for f in forecasts:
                        key = (f.field_id, f.event_type.value)
                        forecast_map.setdefault(key, []).append(f)
                        forecast_dates.add(f.forecast_date)

                    # Consulta en bulk de estados previos
                    alert_ids = [a.id for a in batch]
                    states_map = await uow.breach_states.get_states(alert_ids, list(forecast_dates))

                    # Evaluación fila por fila
                    for alert in batch:
                        if alert.field is None:
                            continue

                        tz = ZoneInfo(alert.field.timezone)
                        rule = AlertRule(
                            id=alert.id,
                            field_id=alert.field_id,
                            user_id=alert.user_id,
                            event_type=alert.event_type,
                            threshold=alert.threshold,
                            timezone=tz,
                            status=alert.status,
                            version=alert.version,
                        )

                        key = (alert.field_id, alert.event_type.value)
                        matching_forecasts = forecast_map.get(key, [])

                        for f in matching_forecasts:
                            point = ForecastPoint(
                                forecast_date=f.forecast_date,
                                event_type=f.event_type,
                                probability=f.probability,
                                issued_at=f.issued_at,
                                source=f.source,
                            )
                            prev_state = states_map.get((alert.id, f.forecast_date))

                            decision = evaluate(
                                rule,
                                point,
                                prev_state,
                                now_utc=now_utc,
                                horizon_days=self._horizon_days,
                                max_age=max_age,
                                rearm_margin=self._rearm_margin,
                            )

                            if isinstance(decision, Fire):
                                fired_count += 1
                                # 1. Actualizar breach_state a is_breached=True con nuevo episodio
                                new_state = BreachState(
                                    alert_id=alert.id,
                                    forecast_date=point.forecast_date,
                                    is_breached=True,
                                    episode=decision.episode,
                                    last_probability=point.probability,
                                    last_issued_at=point.issued_at,
                                )
                                await uow.breach_states.upsert_state(new_state)

                                # 2. Insertar notificación en estado PENDING (idempotencia en DB)
                                notification = Notification(
                                    id=uuid.uuid4(),
                                    alert_id=alert.id,
                                    forecast_date=point.forecast_date,
                                    episode=decision.episode,
                                    event_type=alert.event_type,
                                    probability=point.probability,
                                    threshold=alert.threshold,
                                    issued_at=point.issued_at,
                                    status=NotificationStatus.PENDING,
                                    attempts=0,
                                    next_attempt_at=now_utc,
                                )
                                inserted = await uow.notifications.create_if_not_exists(
                                    notification
                                )
                                if not inserted:
                                    duplicated_count += 1

                            elif isinstance(decision, Hold):
                                held_count += 1

                            elif isinstance(decision, Rearm):
                                # Bloque 4 Bug 10: Persistir Rearm sólo si previo era is_breached
                                if prev_state is not None and prev_state.is_breached:
                                    rearmed_count += 1
                                    new_state = BreachState(
                                        alert_id=alert.id,
                                        forecast_date=point.forecast_date,
                                        is_breached=False,
                                        episode=prev_state.episode,
                                        last_probability=point.probability,
                                        last_issued_at=point.issued_at,
                                    )
                                    await uow.breach_states.upsert_state(new_state)

                            elif isinstance(decision, Skip):
                                if decision.reason == "stale":
                                    stale_count += 1
                                    logger.warning(
                                        "stale_forecast_skipped",
                                        field_id=str(alert.field_id),
                                        event_type=alert.event_type.value,
                                        forecast_date=str(point.forecast_date),
                                        issued_at=point.issued_at.isoformat(),
                                    )
                                elif decision.reason == "out_of_window":
                                    out_of_window_count += 1

                    # Commit por lote
                    await uow.commit()

        finally:
            if self._lock is not None:
                await self._lock.release(EVALUATE_ALERTS_LOCK_ID)

        duration = round(time.monotonic() - start_time, 3)
        report = RunReport(
            run_id=run_id,
            alerts_evaluated=alerts_evaluated,
            fired=fired_count,
            held=held_count,
            rearmed=rearmed_count,
            stale=stale_count,
            out_of_window=out_of_window_count,
            duplicated=duplicated_count,
            duration_seconds=duration,
        )
        logger.info(
            "evaluate_alerts_finished",
            run_id=run_id,
            alerts_evaluated=report.alerts_evaluated,
            fired=report.fired,
            held=report.held,
            rearmed=report.rearmed,
            stale=report.stale,
            out_of_window=report.out_of_window,
            duplicated=report.duplicated,
            duration_seconds=report.duration_seconds,
        )
        return report
