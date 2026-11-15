"""Caso de uso: DispatchNotifications (Job en Background).

Reclama notificaciones pendientes mediante patrón lease (FOR UPDATE SKIP LOCKED)
en una transacción corta, y realiza los envíos fuera de la transacción con concurrencia
acotada, idempotencia y reintentos con backoff exponencial.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from agrobot.application.ports import Clock, NotificationSender, UnitOfWork
from agrobot.config import settings
from agrobot.domain.enums import NotificationStatus
from agrobot.logging import get_logger

logger = get_logger("jobs.dispatch_notifications")


@dataclass(frozen=True)
class DispatchReport:
    run_id: str
    claimed: int
    sent: int
    retried: int
    failed: int
    duration_seconds: float


@dataclass(frozen=True)
class NotificationPayload:
    id: uuid.UUID
    attempts: int
    phone_number: str
    message: str


class DispatchNotifications:
    """Orquestador de entrega de notificaciones."""

    def __init__(
        self,
        uow: UnitOfWork | Callable[[], UnitOfWork],
        sender: NotificationSender,
        clock: Clock,
        batch_size: int = 50,
        lease_duration_s: int | None = None,
        max_attempts: int | None = None,
        concurrency: int | None = None,
    ) -> None:
        if callable(uow):
            self._uow_factory = uow
        else:
            # Si se pasa una instancia de UoW, clonar una fábrica para no compartir sesión
            session_factory = getattr(uow, "_session_factory", None)
            if session_factory is not None:
                uow_cls = cast(Callable[[Any], UnitOfWork], type(uow))
                self._uow_factory = lambda: uow_cls(session_factory)
            else:
                self._uow_factory = lambda: uow

        self._sender = sender
        self._clock = clock
        self._batch_size = batch_size
        self._lease_duration = timedelta(seconds=lease_duration_s or settings.lease_duration_s)
        self._max_attempts = max_attempts or settings.max_attempts
        self._semaphore = asyncio.Semaphore(concurrency or settings.dispatch_concurrency)

    async def run(self) -> DispatchReport:
        run_id = str(uuid.uuid4())
        start_time = time.monotonic()

        # Fase 1: Reclamar notificaciones en una transacción corta
        payloads: list[NotificationPayload] = []
        async with self._uow_factory() as uow:
            claimed_records = await uow.notifications.claim_pending(
                limit=self._batch_size,
                lease_duration=self._lease_duration,
            )
            for notif in claimed_records:
                phone = (
                    notif.alert.user.phone_number
                    if (notif.alert and notif.alert.user)
                    else "+5490000000000"
                )
                msg = (
                    f"Alerta meteorológica: riesgo de {notif.event_type.value} "
                    f"para el {notif.forecast_date}. Probabilidad: {notif.probability}% "
                    f"(umbral configurado: {notif.threshold}%)."
                )
                payloads.append(
                    NotificationPayload(
                        id=notif.id,
                        attempts=notif.attempts,
                        phone_number=phone,
                        message=msg,
                    )
                )
            await uow.commit()

        if not payloads:
            return DispatchReport(
                run_id=run_id,
                claimed=0,
                sent=0,
                retried=0,
                failed=0,
                duration_seconds=round(time.monotonic() - start_time, 3),
            )

        logger.info(
            "dispatch_claimed_notifications",
            run_id=run_id,
            count=len(payloads),
        )

        sent_count = 0
        retried_count = 0
        failed_count = 0

        # Fase 2: Enviar fuera de la transacción con concurrencia controlada
        async def _dispatch_single(item: NotificationPayload) -> None:
            nonlocal sent_count, retried_count, failed_count
            async with self._semaphore:
                try:
                    success = await self._sender.send(
                        notification_id=item.id,
                        phone_number=item.phone_number,
                        message=item.message,
                    )
                    if not success:
                        raise RuntimeError("Notification sender returned failure")

                    # Éxito: marcar como SENT
                    async with self._uow_factory() as uow:
                        await uow.notifications.update_status(
                            notification_id=item.id,
                            status=NotificationStatus.SENT,
                            attempts=item.attempts,
                            sent_at=self._clock.now_utc(),
                            last_error=None,
                        )
                        await uow.commit()
                    sent_count += 1

                except Exception as exc:
                    err_msg = str(exc)
                    now = self._clock.now_utc()

                    if item.attempts >= self._max_attempts:
                        # Se agotaron los reintentos -> FAILED
                        async with self._uow_factory() as uow:
                            await uow.notifications.update_status(
                                notification_id=item.id,
                                status=NotificationStatus.FAILED,
                                attempts=item.attempts,
                                last_error=err_msg,
                            )
                            await uow.commit()
                        failed_count += 1
                        logger.error(
                            "notification_permanently_failed",
                            notification_id=str(item.id),
                            attempts=item.attempts,
                            error=err_msg,
                        )
                    else:
                        # Backoff exponencial con jitter: (2^attempts * 5) + jitter [0..5]s
                        backoff = (2**item.attempts) * 5 + random.uniform(0, 5)
                        next_attempt = now + timedelta(seconds=backoff)

                        async with self._uow_factory() as uow:
                            await uow.notifications.update_status(
                                notification_id=item.id,
                                status=NotificationStatus.PENDING,
                                attempts=item.attempts,
                                next_attempt_at=next_attempt,
                                last_error=err_msg,
                            )
                            await uow.commit()
                        retried_count += 1
                        logger.warning(
                            "notification_dispatch_retry_scheduled",
                            notification_id=str(item.id),
                            attempts=item.attempts,
                            next_attempt_at=next_attempt.isoformat(),
                            error=err_msg,
                        )

        await asyncio.gather(*[_dispatch_single(p) for p in payloads])

        duration = time.monotonic() - start_time
        logger.info(
            "dispatch_finished",
            run_id=run_id,
            sent=sent_count,
            retried=retried_count,
            failed=failed_count,
            duration_s=round(duration, 3),
        )
        return DispatchReport(
            run_id=run_id,
            claimed=len(payloads),
            sent=sent_count,
            retried=retried_count,
            failed=failed_count,
            duration_seconds=duration,
        )
