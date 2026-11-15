"""Adaptador NotificationSender mock que loguea el mensaje (simulación de WhatsApp)."""

import uuid

from agrobot.logging import get_logger

logger = get_logger("notifications.sender")


class LoggingSender:
    """Emisor mock que loguea las notificaciones estructuradas.

    Permite inyectar fallas controladas para testing (AC-12, AC-13).
    """

    def __init__(self, failures_before_success: int = 0, always_fail: bool = False) -> None:
        self._failures_before_success = failures_before_success
        self._always_fail = always_fail
        self._attempt_counts: dict[uuid.UUID, int] = {}

    async def send(self, notification_id: uuid.UUID, phone_number: str, message: str) -> bool:
        """Simula el envío hacia WhatsApp o un proveedor SMS."""
        current_attempts = self._attempt_counts.get(notification_id, 0) + 1
        self._attempt_counts[notification_id] = current_attempts

        if self._always_fail:
            logger.warning(
                "notification_send_failed_simulated",
                notification_id=str(notification_id),
                phone_number=phone_number,
                attempt=current_attempts,
                reason="simulated permanent network error",
            )
            raise RuntimeError("Provider error: WhatsApp Gateway 503 Service Unavailable")

        if current_attempts <= self._failures_before_success:
            logger.warning(
                "notification_send_transient_failure",
                notification_id=str(notification_id),
                phone_number=phone_number,
                attempt=current_attempts,
                remaining_failures=self._failures_before_success - current_attempts,
            )
            raise ConnectionError("Transient network timeout connecting to WhatsApp Gateway")

        logger.info(
            "notification_sent_success",
            notification_id=str(notification_id),
            phone_number=phone_number,
            message=message,
            attempt=current_attempts,
        )
        return True
