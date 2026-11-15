"""Implementaciones del puerto Clock."""

from datetime import UTC, datetime


class SystemClock:
    """Reloj del sistema que devuelve el tiempo UTC real."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Reloj determinista para tests."""

    def __init__(self, current_time: datetime) -> None:
        self._current_time = current_time

    def set_time(self, new_time: datetime) -> None:
        self._current_time = new_time

    def now_utc(self) -> datetime:
        return self._current_time
