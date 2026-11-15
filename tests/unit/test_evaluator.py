"""Tests unitarios y basados en propiedades (Hypothesis) para evaluate().

Cubre AC-01, AC-02, AC-03, AC-04, AC-04b, AC-04c, AC-04e, AC-06, AC-07, AC-08, AC-09, AC-10
y el comportamiento de REARM_MARGIN.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from agrobot.domain.entities import (
    AlertRule,
    BreachState,
    Fire,
    ForecastPoint,
    Hold,
    Rearm,
    Skip,
)
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.evaluator import evaluate


def make_rule(
    *,
    event_type: WeatherEvent = WeatherEvent.FROST,
    threshold: Decimal = Decimal("60.00"),
    tz_name: str = "America/Argentina/Cordoba",
    status: AlertStatus = AlertStatus.ACTIVE,
    deleted_at: datetime | None = None,
) -> AlertRule:
    return AlertRule(
        id=uuid.uuid4(),
        field_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        event_type=event_type,
        threshold=threshold,
        timezone=ZoneInfo(tz_name),
        status=status,
        deleted_at=deleted_at,
    )


def test_ac_01_inclusive_boundary_breach() -> None:
    """AC-01: Alerta frost >= 60 y pronóstico frost 60.00 para mañana -> Fire(episode=1)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(event_type=WeatherEvent.FROST, threshold=Decimal("60.00"))
    forecast = ForecastPoint(
        forecast_date=date(2026, 5, 11),
        event_type=WeatherEvent.FROST,
        probability=Decimal("60.00"),
        issued_at=now_utc - timedelta(hours=2),
    )

    decision = evaluate(rule, forecast, None, now_utc=now_utc)
    assert isinstance(decision, Fire)
    assert decision.episode == 1


def test_ac_02_below_threshold_no_breach() -> None:
    """AC-02: Igual que AC-01 con 59.99 -> Rearm (sin disparo)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(event_type=WeatherEvent.FROST, threshold=Decimal("60.00"))
    forecast = ForecastPoint(
        forecast_date=date(2026, 5, 11),
        event_type=WeatherEvent.FROST,
        probability=Decimal("59.99"),
        issued_at=now_utc - timedelta(hours=2),
    )

    decision = evaluate(rule, forecast, None, now_utc=now_utc)
    assert isinstance(decision, Rearm)


def test_ac_03_repeated_evaluation_without_change_holds() -> None:
    """AC-03: El evaluador corre 2 veces seguidas sin cambios -> Hold (no duplica aviso)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("60.00"))
    forecast = ForecastPoint(
        forecast_date=date(2026, 5, 11),
        event_type=WeatherEvent.FROST,
        probability=Decimal("70.00"),
        issued_at=now_utc - timedelta(hours=2),
    )

    # Primera corrida: Fire(1)
    d1 = evaluate(rule, forecast, None, now_utc=now_utc)
    assert isinstance(d1, Fire)
    assert d1.episode == 1

    # Estado resultante guardado
    state = BreachState(
        alert_id=rule.id,
        forecast_date=forecast.forecast_date,
        is_breached=True,
        episode=1,
        last_probability=forecast.probability,
        last_issued_at=forecast.issued_at,
    )

    # Segunda corrida sin cambios
    d2 = evaluate(rule, forecast, state, now_utc=now_utc)
    assert isinstance(d2, Hold)


def test_ac_04_oscillation_90_30_90_rearms_and_fires_episode_2() -> None:
    """AC-04: Pronóstico 90 -> 30 -> 90 para la misma fecha -> 2 disparos (episodios 1 y 2)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("60.00"))
    forecast_date = date(2026, 5, 11)

    # Paso 1: 90%
    f1 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=3)
    )
    d1 = evaluate(rule, f1, None, now_utc=now_utc)
    assert isinstance(d1, Fire)
    assert d1.episode == 1

    state1 = BreachState(rule.id, forecast_date, True, 1, f1.probability, f1.issued_at)

    # Paso 2: 30% (baja del umbral -> Rearm)
    f2 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("30.00"), now_utc - timedelta(hours=2)
    )
    d2 = evaluate(rule, f2, state1, now_utc=now_utc)
    assert isinstance(d2, Rearm)

    state2 = BreachState(rule.id, forecast_date, False, 1, f2.probability, f2.issued_at)

    # Paso 3: 90% (vuelve a subir -> nuevo cruce, Fire episode 2)
    f3 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )
    d3 = evaluate(rule, f3, state2, now_utc=now_utc)
    assert isinstance(d3, Fire)
    assert d3.episode == 2


def test_ac_04b_increasing_probability_70_85_95_holds() -> None:
    """AC-04b: Pronóstico 70 -> 85 -> 95 (sigue arriba) -> 1 notificación (Fire(1), Hold, Hold)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("60.00"))
    forecast_date = date(2026, 5, 11)

    # Paso 1: 70%
    f1 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("70.00"), now_utc - timedelta(hours=3)
    )
    d1 = evaluate(rule, f1, None, now_utc=now_utc)
    assert isinstance(d1, Fire)
    assert d1.episode == 1

    state1 = BreachState(rule.id, forecast_date, True, 1, f1.probability, f1.issued_at)

    # Paso 2: 85%
    f2 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("85.00"), now_utc - timedelta(hours=2)
    )
    d2 = evaluate(rule, f2, state1, now_utc=now_utc)
    assert isinstance(d2, Hold)

    # Paso 3: 95%
    f3 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("95.00"), now_utc - timedelta(hours=1)
    )
    d3 = evaluate(rule, f3, state1, now_utc=now_utc)
    assert isinstance(d3, Hold)


def test_ac_04c_stale_does_not_count_as_drop() -> None:
    """AC-04c: Pronóstico 90 -> stale -> 90 -> 1 notificación (stale no re-arma)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("60.00"))
    forecast_date = date(2026, 5, 11)

    # Paso 1: 90%
    f1 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=2)
    )
    d1 = evaluate(rule, f1, None, now_utc=now_utc)
    assert isinstance(d1, Fire)
    assert d1.episode == 1

    state1 = BreachState(rule.id, forecast_date, True, 1, f1.probability, f1.issued_at)

    # Paso 2: stale (emitido hace 25h) -> Skip("stale"), no toca el estado
    f_stale = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("10.00"), now_utc - timedelta(hours=25)
    )
    d_stale = evaluate(rule, f_stale, state1, now_utc=now_utc)
    assert isinstance(d_stale, Skip)
    assert d_stale.reason == "stale"

    # Paso 3: 90% vigente de nuevo -> Hold (el estado sigue siendo is_breached=True)
    f3 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )
    d3 = evaluate(rule, f3, state1, now_utc=now_utc)
    assert isinstance(d3, Hold)


def test_ac_04e_field_timezone_calculation() -> None:
    """AC-04e: Campo en Asia/Tokyo (+09:00), servidor a las 20:00 UTC (10 de mayo).

    En Tokyo ya son las 05:00 del 11 de mayo -> "hoy" del campo es 11 de mayo.
    Una fecha del 10 de mayo en el campo ya es pasada (out_of_window).
    """
    now_utc = datetime(2026, 5, 10, 20, 0, tzinfo=UTC)
    rule = make_rule(tz_name="Asia/Tokyo")

    # 10 de mayo en Tokyo ya pasó
    f_past = ForecastPoint(
        date(2026, 5, 10), WeatherEvent.FROST, Decimal("80.00"), now_utc - timedelta(hours=1)
    )
    d_past = evaluate(rule, f_past, None, now_utc=now_utc)
    assert isinstance(d_past, Skip)
    assert d_past.reason == "out_of_window"

    # 11 de mayo en Tokyo es "hoy", por tanto entra en ventana
    f_today = ForecastPoint(
        date(2026, 5, 11), WeatherEvent.FROST, Decimal("80.00"), now_utc - timedelta(hours=1)
    )
    d_today = evaluate(rule, f_today, None, now_utc=now_utc)
    assert isinstance(d_today, Fire)


def test_ac_06_breach_across_different_forecast_dates() -> None:
    """AC-06: Pronóstico >= umbral en 3 fechas distintas -> 3 decisiones Fire(1)."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("50.00"))

    for day_offset in (1, 2, 3):
        f = ForecastPoint(
            forecast_date=date(2026, 5, 10) + timedelta(days=day_offset),
            event_type=WeatherEvent.FROST,
            probability=Decimal("75.00"),
            issued_at=now_utc - timedelta(hours=1),
        )
        decision = evaluate(rule, f, None, now_utc=now_utc)
        assert isinstance(decision, Fire)
        assert decision.episode == 1


def test_ac_07_paused_or_deleted_alert_skipped() -> None:
    """AC-07: Alerta pausada o borrada -> 0 notificaciones (Skip('inactive_rule'))."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    forecast = ForecastPoint(
        date(2026, 5, 11), WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )

    # Pausada
    rule_paused = make_rule(status=AlertStatus.PAUSED)
    assert evaluate(rule_paused, forecast, None, now_utc=now_utc) == Skip(reason="inactive_rule")

    # Borrada
    rule_deleted = make_rule(deleted_at=now_utc - timedelta(days=1))
    assert evaluate(rule_deleted, forecast, None, now_utc=now_utc) == Skip(reason="inactive_rule")


def test_ac_08_past_date_or_beyond_horizon_skipped() -> None:
    """AC-08: Pronóstico de ayer o fuera del horizonte -> out_of_window."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule()

    # Fecha de ayer
    f_yesterday = ForecastPoint(
        date(2026, 5, 9), WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )
    assert evaluate(rule, f_yesterday, None, now_utc=now_utc, horizon_days=7) == Skip(
        reason="out_of_window"
    )

    # Fecha posterior al horizonte (+8 días)
    f_far_future = ForecastPoint(
        date(2026, 5, 18), WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )
    assert evaluate(rule, f_far_future, None, now_utc=now_utc, horizon_days=7) == Skip(
        reason="out_of_window"
    )


def test_ac_09_stale_forecast_skipped() -> None:
    """AC-09: Pronóstico con issued_at mayor a MAX_FORECAST_AGE_HOURS (24h) -> Skip('stale')."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule()
    f_stale = ForecastPoint(
        date(2026, 5, 11),
        WeatherEvent.FROST,
        Decimal("85.00"),
        issued_at=now_utc - timedelta(hours=24, minutes=1),
    )

    decision = evaluate(rule, f_stale, None, now_utc=now_utc, max_age=timedelta(hours=24))
    assert isinstance(decision, Skip)
    assert decision.reason == "stale"


def test_ac_10_mismatched_weather_event_skipped() -> None:
    """AC-10: Alerta de otro evento en el mismo campo -> event_mismatch."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(event_type=WeatherEvent.RAIN)
    forecast = ForecastPoint(
        date(2026, 5, 11), WeatherEvent.FROST, Decimal("90.00"), now_utc - timedelta(hours=1)
    )

    assert evaluate(rule, forecast, None, now_utc=now_utc) == Skip(reason="event_mismatch")


def test_rearm_margin_anti_flapping() -> None:
    """REARM_MARGIN: con threshold=60 y margin=5, re-arma recién cuando baja de 55."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("60.00"))
    forecast_date = date(2026, 5, 11)
    margin = Decimal("5.00")

    # Paso 1: Disparo inicial con 65%
    f1 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("65.00"), now_utc - timedelta(hours=3)
    )
    d1 = evaluate(rule, f1, None, now_utc=now_utc, rearm_margin=margin)
    assert isinstance(d1, Fire)
    assert d1.episode == 1

    state = BreachState(rule.id, forecast_date, True, 1, f1.probability, f1.issued_at)

    # Paso 2: Baja a 57% (< 60 pero >= 55). No re-arma -> Hold
    f2 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("57.00"), now_utc - timedelta(hours=2)
    )
    d2 = evaluate(rule, f2, state, now_utc=now_utc, rearm_margin=margin)
    assert isinstance(d2, Hold)

    # Paso 3: Baja a 54% (< 55). Cruza el margen -> Rearm
    f3 = ForecastPoint(
        forecast_date, WeatherEvent.FROST, Decimal("54.00"), now_utc - timedelta(hours=1)
    )
    d3 = evaluate(rule, f3, state, now_utc=now_utc, rearm_margin=margin)
    assert isinstance(d3, Rearm)


# --------------------------------------------------------------------------- #
# Tests basados en propiedades (Hypothesis)
# --------------------------------------------------------------------------- #
@given(
    threshold=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100.00"), places=2),
    probability=st.decimals(min_value=Decimal("0.00"), max_value=Decimal("99.99"), places=2),
)
def test_property_never_fire_if_prob_strictly_below_threshold(
    threshold: Decimal, probability: Decimal
) -> None:
    """Propiedad 1: Nunca hay Fire si probability < threshold."""
    if probability >= threshold:
        return

    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=threshold)
    forecast = ForecastPoint(
        date(2026, 5, 11), WeatherEvent.FROST, probability, now_utc - timedelta(hours=1)
    )

    decision = evaluate(rule, forecast, None, now_utc=now_utc)
    assert not isinstance(decision, Fire)


@given(
    threshold=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100.00"), places=2),
    prob=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100.00"), places=2),
)
def test_property_idempotent_evaluation_same_data_never_fires_twice(
    threshold: Decimal, prob: Decimal
) -> None:
    """Propiedad 2: Aplicar dos veces el mismo dato no produce un segundo Fire."""
    if prob < threshold:
        return

    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=threshold)
    forecast = ForecastPoint(
        date(2026, 5, 11), WeatherEvent.FROST, prob, now_utc - timedelta(hours=1)
    )

    d1 = evaluate(rule, forecast, None, now_utc=now_utc)
    assert isinstance(d1, Fire)

    state = BreachState(rule.id, forecast.forecast_date, True, d1.episode, prob, forecast.issued_at)

    d2 = evaluate(rule, forecast, state, now_utc=now_utc)
    assert isinstance(d2, Hold)


@given(
    st.lists(
        st.decimals(min_value=Decimal("0.00"), max_value=Decimal("100.00"), places=2),
        min_size=2,
        max_size=20,
    )
)
def test_property_episode_monotonically_increasing(probabilities: list[Decimal]) -> None:
    """Propiedad 3: El episode es monótono creciente a lo largo de cualquier secuencia."""
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    rule = make_rule(threshold=Decimal("50.00"))
    forecast_date = date(2026, 5, 11)

    state: BreachState | None = None
    last_episode = 0

    for i, prob in enumerate(probabilities):
        forecast = ForecastPoint(
            forecast_date,
            WeatherEvent.FROST,
            prob,
            now_utc - timedelta(minutes=10 * (len(probabilities) - i)),
        )
        decision = evaluate(rule, forecast, state, now_utc=now_utc)

        if isinstance(decision, Fire):
            assert decision.episode > last_episode
            assert decision.episode == last_episode + 1
            last_episode = decision.episode
            state = BreachState(
                rule.id, forecast_date, True, decision.episode, prob, forecast.issued_at
            )
        elif isinstance(decision, Rearm):
            episode = state.episode if state else 0
            state = BreachState(rule.id, forecast_date, False, episode, prob, forecast.issued_at)
        elif isinstance(decision, Hold):
            assert state is not None and state.is_breached
            state = BreachState(
                rule.id, forecast_date, True, state.episode, prob, forecast.issued_at
            )
