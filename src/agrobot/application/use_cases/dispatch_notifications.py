"""Caso de uso: DispatchNotifications (Outbox Worker con Lease Pattern).

Reclama notificaciones pendientes con FOR UPDATE SKIP LOCKED y despacha a WhatsApp
en paralelo acotado por un semáforo asincrónico.
"""

from __future__ import annotations

import asyncio
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
    """Reporte estructurado de la corrida de despacho de notificaciones."""

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
    phone_number: str | None
    message: str


class DispatchNotifications:
    """Orquestador del envío confiable de notificaciones desde el outbox."""

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
        now_utc = self._clock.now_utc()

        # Fase 1: Reclamar notificaciones en una transacción corta pasando el reloj actual
        payloads: list[NotificationPayload] = []
        async with self._uow_factory() as uow:
            claimed_records = await uow.notifications.claim_pending(
                limit=self._batch_size,
                lease_duration=self._lease_duration,
                now=now_utc,
            )
            for notif in claimed_records:
                msg = (
                    f"Alerta meteorológica: riesgo de {notif.event_type.value} "
                    f"para el {notif.forecast_date}. Probabilidad: {notif.probability}% "
                    f"(umbral configurado: {notif.threshold}%)."
                )
                payloads.append(
                    NotificationPayload(
                        id=notif.id,
                        attempts=notif.attempts,
                        phone_number=notif.recipient_phone,
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

            # Bloque 4 Bug 8: Si no hay teléfono destinatario, marcar failed sin llamar al sender
            if not item.phone_number:
                logger.warning(
                    "dispatch_missing_recipient_phone",
                    notification_id=str(item.id),
                )
                async with self._uow_factory() as uow:
                    await uow.notifications.update_status(
                        notification_id=item.id,
                        status=NotificationStatus.FAILED,
                        attempts=item.attempts,
                        sent_at=None,
                        last_error="missing_recipient",
                    )
                    await uow.commit()
                failed_count += 1
                return

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
                                sent_at=None,
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
                        # Backoff exponencial: 2^(attempts-1) * 30 segundos
                        backoff_seconds = (2 ** (item.attempts - 1)) * 30
                        next_attempt = now + timedelta(seconds=backoff_seconds)
                        async with self._uow_factory() as uow:
                            await uow.notifications.update_status(
                                notification_id=item.id,
                                status=NotificationStatus.PENDING,
                                attempts=item.attempts,
                                next_attempt_at=next_attempt,
                                last_error=err_msg,
                                sent_at=None,
                            )
                            await uow.commit()
                        retried_count += 1
                        logger.warning(
                            "notification_retrying_backoff",
                            notification_id=str(item.id),
                            attempts=item.attempts,
                            next_attempt_at=next_attempt.isoformat(),
                            error=err_msg,
                        )

        # Ejecutar envíos en paralelo acotado
        await asyncio.gather(*[_dispatch_single(p) for p in payloads])

        duration = round(time.monotonic() - start_time, 3)
        report = DispatchReport(
            run_id=run_id,
            claimed=len(payloads),
            sent=sent_count,
            retried=retried_count,
            failed=failed_count,
            duration_seconds=duration,
        )
        logger.info(
            "dispatch_finished",
            run_id=run_id,
            claimed=report.claimed,
            sent=report.sent,
            retried=report.retried,
            failed=report.failed,
            duration_seconds=report.duration_seconds,
        )
        return report
