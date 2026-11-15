"""Lógica pura de evaluación de alertas climáticas (sin I/O ni dependencias externas).

Implementa la máquina de estados por (alerta, forecast_date) con re-aviso por episodio.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from agrobot.domain.entities import (
    AlertRule,
    BreachState,
    Decision,
    Fire,
    ForecastPoint,
    Hold,
    Rearm,
    Skip,
)


def evaluate(
    rule: AlertRule,
    forecast: ForecastPoint,
    prev_state: BreachState | None,
    *,
    now_utc: datetime,
    horizon_days: int = 7,
    max_age: timedelta = timedelta(hours=24),
    rearm_margin: Decimal = Decimal("0.00"),
) -> Decision:
    """Evalúa un punto de pronóstico para una regla y retorna la decisión de transición.

    Reglas de negocio:
    - Alerta inactiva o borrada -> Skip("inactive_rule")
    - Evento climático distinto -> Skip("event_mismatch")
    - Fecha fuera de [hoy_campo, hoy_campo + horizon_days] -> Skip("out_of_window")
    - Pronóstico no vigente (now - issued_at > max_age) -> Skip("stale")
    - probability >= threshold y (sin estado previo o no disparada) -> Fire(episode = prev + 1)
    - probability >= threshold y ya disparada -> Hold()
    - probability < (threshold - rearm_margin) -> Rearm()
    - Dentro de la zona muerta [threshold - rearm_margin, threshold): mantiene el estado previo.
    """
    if not rule.is_active:
        return Skip(reason="inactive_rule")

    if rule.event_type != forecast.event_type:
        return Skip(reason="event_mismatch")

    # Fecha local actual del campo en su zona horaria
    today_field = now_utc.astimezone(rule.timezone).date()
    window_end = today_field + timedelta(days=horizon_days)

    if forecast.forecast_date < today_field or forecast.forecast_date > window_end:
        return Skip(reason="out_of_window")

    # Validación de frescura según issued_at del proveedor
    age = now_utc - forecast.issued_at
    if age > max_age:
        return Skip(reason="stale")

    # Comparación exacta con Decimal
    if forecast.probability >= rule.threshold:
        if prev_state is None or not prev_state.is_breached:
            next_episode = (prev_state.episode + 1) if prev_state is not None else 1
            return Fire(episode=next_episode)
        return Hold()

    rearm_threshold = rule.threshold - rearm_margin
    if forecast.probability < rearm_threshold:
        return Rearm()

    # Zona muerta de histéresis: si ya estaba disparada, no se re-arma todavía
    if prev_state is not None and prev_state.is_breached:
        return Hold()
    return Rearm()
