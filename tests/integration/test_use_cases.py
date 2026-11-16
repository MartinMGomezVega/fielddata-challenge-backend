"""Tests de integración contra PostgreSQL real para los casos de uso principales.

Cubre:
- AC-03: Correr el evaluador 2 veces sin cambios -> 1 sola notificación.
- AC-04: Oscilación 90 -> 30 -> 90 de punta a punta -> 2 notificaciones (episodios 1 y 2).
- AC-04d: Upsert de pronóstico con issued_at viejo no retrocede el valor guardado.
- AC-11: Dos evaluadores en paralelo con asyncio.gather -> advisory lock evita duplicados.
- AC-12: Sender falla 1 vez y luego funciona -> sent con attempts = 2.
- AC-13: Sender falla siempre -> failed tras MAX_ATTEMPTS con last_error.
- Concurrencia de dispatchers: Dos dispatchers en paralelo nunca procesan la misma fila.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agrobot.application.use_cases.dispatch_notifications import DispatchNotifications
from agrobot.application.use_cases.evaluate_alerts import EvaluateAlerts
from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent
from agrobot.infrastructure.clock import FrozenClock
from agrobot.infrastructure.db.locks import PostgresAdvisoryLock
from agrobot.infrastructure.db.models import (
    Alert,
    AlertBreachState,
    AlertNotification,
    Field,
    User,
    WeatherForecast,
)
from agrobot.infrastructure.db.uow import SqlAlchemyUnitOfWork
from agrobot.infrastructure.notifications.logging_sender import LoggingSender

TEST_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/agrobot_test"
)


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(TEST_DB_URL, echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=async_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def clean_db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async with session_factory() as session:
        await session.execute(delete(AlertNotification))
        await session.execute(delete(AlertBreachState))
        await session.execute(delete(Alert))
        await session.execute(delete(WeatherForecast))
        await session.execute(delete(Field))
        await session.execute(delete(User))
        await session.commit()
    yield
    async with session_factory() as session:
        await session.execute(delete(AlertNotification))
        await session.execute(delete(AlertBreachState))
        await session.execute(delete(Alert))
        await session.execute(delete(WeatherForecast))
        await session.execute(delete(Field))
        await session.execute(delete(User))
        await session.commit()


@pytest.fixture
async def seeded_entities(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> dict[str, uuid.UUID]:
    _ = clean_db
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    alert_id = uuid.uuid4()

    async with session_factory() as session:
        user = User(id=user_id, name="Test User", phone_number="+5491199887766")
        field = Field(
            id=field_id,
            owner_id=user_id,
            name="Campo Central",
            latitude=Decimal("-31.42"),
            longitude=Decimal("-64.18"),
            timezone="America/Argentina/Cordoba",
        )
        alert = Alert(
            id=alert_id,
            user_id=user_id,
            field_id=field_id,
            event_type=WeatherEvent.FROST,
            threshold=Decimal("60.00"),
            status=AlertStatus.ACTIVE,
            version=1,
        )
        session.add_all([user, field, alert])
        await session.commit()

    return {"user_id": user_id, "field_id": field_id, "alert_id": alert_id}


async def test_ac_03_repeated_evaluation_without_changes(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-03: Correr el evaluador 2 veces seguidas sin cambios -> 1 sola notificación."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow = SqlAlchemyUnitOfWork(session_factory)

    # Insertar pronóstico 75%
    async with uow as u:
        f = WeatherForecast(
            field_id=entities["field_id"],
            forecast_date=target_date,
            event_type=WeatherEvent.FROST,
            probability=Decimal("75.00"),
            issued_at=now_utc - timedelta(hours=2),
        )
        await u.forecasts.upsert(f)
        await u.commit()

    async with session_factory() as session:
        lock = PostgresAdvisoryLock(session)
        evaluator = EvaluateAlerts(uow, clock, lock)

        # Primera corrida: dispara
        r1 = await evaluator.run()
        assert r1.fired == 1
        assert r1.held == 0

        # Segunda corrida: no genera nueva notificación (Hold)
        r2 = await evaluator.run()
        assert r2.fired == 0
        assert r2.held == 1

        # Verificar total de notificaciones en base
        notif_count = await session.scalar(
            select(func.count(AlertNotification.id)).where(
                AlertNotification.alert_id == entities["alert_id"]
            )
        )
        assert notif_count == 1


async def test_ac_04_oscillation_90_30_90_end_to_end(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-04: Pronóstico 90 -> 30 -> 90 para la misma fecha.
    Genera 2 notificaciones (episodios 1 y 2).
    """
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow = SqlAlchemyUnitOfWork(session_factory)

    # 1. Pronóstico 90% (cruza el umbral 60%)
    async with uow as u:
        await u.forecasts.upsert(
            WeatherForecast(
                field_id=entities["field_id"],
                forecast_date=target_date,
                event_type=WeatherEvent.FROST,
                probability=Decimal("90.00"),
                issued_at=now_utc - timedelta(hours=3),
            )
        )
        await u.commit()

    async with session_factory() as session:
        lock = PostgresAdvisoryLock(session)
        evaluator = EvaluateAlerts(uow, clock, lock)
        r1 = await evaluator.run()
        assert r1.fired == 1

    # 2. Pronóstico baja a 30% (rearma)
    async with uow as u:
        await u.forecasts.upsert(
            WeatherForecast(
                field_id=entities["field_id"],
                forecast_date=target_date,
                event_type=WeatherEvent.FROST,
                probability=Decimal("30.00"),
                issued_at=now_utc - timedelta(hours=2),
            )
        )
        await u.commit()

    async with session_factory() as session:
        lock = PostgresAdvisoryLock(session)
        evaluator = EvaluateAlerts(uow, clock, lock)
        r2 = await evaluator.run()
        assert r2.rearmed == 1
        assert r2.fired == 0

    # 3. Pronóstico vuelve a subir a 90% (nuevo episodio -> re-avisa)
    async with uow as u:
        await u.forecasts.upsert(
            WeatherForecast(
                field_id=entities["field_id"],
                forecast_date=target_date,
                event_type=WeatherEvent.FROST,
                probability=Decimal("90.00"),
                issued_at=now_utc - timedelta(hours=1),
            )
        )
        await u.commit()

    async with session_factory() as session:
        lock = PostgresAdvisoryLock(session)
        evaluator = EvaluateAlerts(uow, clock, lock)
        r3 = await evaluator.run()
        assert r3.fired == 1

    # Verificar que existen 2 notificaciones con episodios 1 y 2
    async with session_factory() as session:
        notifs = (
            await session.scalars(
                select(AlertNotification)
                .where(AlertNotification.alert_id == entities["alert_id"])
                .order_by(AlertNotification.episode.asc())
            )
        ).all()
        assert len(notifs) == 2
        assert notifs[0].episode == 1
        assert notifs[1].episode == 2


async def test_ac_04d_upsert_with_older_issued_at_is_ignored(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-04d: Un lote con issued_at más viejo que el guardado no altera el pronóstico."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    target_date = date(2026, 5, 11)
    uow = SqlAlchemyUnitOfWork(session_factory)

    # 1. Llega pronóstico con issued_at = now - 2h
    f1 = WeatherForecast(
        field_id=entities["field_id"],
        forecast_date=target_date,
        event_type=WeatherEvent.FROST,
        probability=Decimal("80.00"),
        issued_at=now_utc - timedelta(hours=2),
    )
    async with uow as u:
        await u.forecasts.upsert(f1)
        await u.commit()

    # 2. Llega un lote desordenado con issued_at = now - 5h (más viejo)
    f_old = WeatherForecast(
        field_id=entities["field_id"],
        forecast_date=target_date,
        event_type=WeatherEvent.FROST,
        probability=Decimal("20.00"),
        issued_at=now_utc - timedelta(hours=5),
    )
    async with uow as u:
        updated = await u.forecasts.upsert(f_old)
        await u.commit()
        assert not updated  # No debió actualizar

    # 3. Comprobar que en la base sigue el 80.00
    async with session_factory() as session:
        stored = await session.scalar(
            select(WeatherForecast).where(
                WeatherForecast.field_id == entities["field_id"],
                WeatherForecast.forecast_date == target_date,
                WeatherForecast.event_type == WeatherEvent.FROST,
            )
        )
        assert stored is not None
        assert stored.probability == Decimal("80.00")
        assert stored.issued_at == f1.issued_at


async def test_ac_11_concurrent_evaluators_advisory_lock(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-11: Dos workers corren el evaluador a la vez -> mismo resultado que uno solo."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow1 = SqlAlchemyUnitOfWork(session_factory)
    uow2 = SqlAlchemyUnitOfWork(session_factory)

    # Pronóstico 70%
    async with uow1 as u:
        await u.forecasts.upsert(
            WeatherForecast(
                field_id=entities["field_id"],
                forecast_date=target_date,
                event_type=WeatherEvent.FROST,
                probability=Decimal("70.00"),
                issued_at=now_utc - timedelta(hours=1),
            )
        )
        await u.commit()

    async with session_factory() as s1, session_factory() as s2:
        lock1 = PostgresAdvisoryLock(s1)
        lock2 = PostgresAdvisoryLock(s2)

        ev1 = EvaluateAlerts(uow1, clock, lock1)
        ev2 = EvaluateAlerts(uow2, clock, lock2)

        # Correr ambos al mismo tiempo
        r1, r2 = await asyncio.gather(ev1.run(), ev2.run())

        # Exactamente uno corrió y el otro fue salteado
        skipped = [r for r in (r1, r2) if r.skipped]
        completed = [r for r in (r1, r2) if not r.skipped]
        assert len(skipped) == 1
        assert len(completed) == 1
        assert completed[0].fired == 1


async def test_ac_12_sender_transient_failure_and_retry(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-12: Sender falla 1 vez y luego funciona -> notificación sent, attempts = 2."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow = SqlAlchemyUnitOfWork(session_factory)

    # Crear notificación pendiente
    notif_id = uuid.uuid4()
    notif = AlertNotification(
        id=notif_id,
        alert_id=entities["alert_id"],
        forecast_date=target_date,
        episode=1,
        event_type=WeatherEvent.FROST,
        probability=Decimal("70.00"),
        threshold=Decimal("60.00"),
        issued_at=now_utc - timedelta(hours=1),
        status=NotificationStatus.PENDING,
        attempts=0,
        next_attempt_at=now_utc,
    )
    async with uow as u:
        await u.notifications.create_if_not_exists(notif)
        await u.commit()

    # Sender configurado para fallar 1 vez
    sender = LoggingSender(failures_before_success=1)
    dispatcher = DispatchNotifications(uow, sender, clock, max_attempts=3)

    # Primer intento: falla y reprograma con backoff
    r1 = await dispatcher.run()
    assert r1.claimed == 1
    assert r1.sent == 0
    assert r1.retried == 1

    async with session_factory() as session:
        notif_db = await session.get(AlertNotification, notif_id)
        assert notif_db is not None
        assert notif_db.status == NotificationStatus.PENDING
        assert notif_db.attempts == 1
        assert notif_db.last_error is not None
        # Avanzar el reloj para superar el backoff
        clock.set_time(notif_db.next_attempt_at + timedelta(seconds=1))

    # Segundo intento: éxito
    r2 = await dispatcher.run()
    assert r2.claimed == 1
    assert r2.sent == 1

    async with session_factory() as session:
        notif_db = await session.get(AlertNotification, notif_id)
        assert notif_db is not None
        assert notif_db.status == NotificationStatus.SENT
        assert notif_db.attempts == 2
        assert notif_db.sent_at is not None


async def test_ac_13_sender_permanent_failure(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """AC-13: Sender falla siempre -> failed tras MAX_ATTEMPTS, last_error con causa."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow = SqlAlchemyUnitOfWork(session_factory)
    notif_id = uuid.uuid4()
    notif = AlertNotification(
        id=notif_id,
        alert_id=entities["alert_id"],
        forecast_date=target_date,
        episode=1,
        event_type=WeatherEvent.FROST,
        probability=Decimal("70.00"),
        threshold=Decimal("60.00"),
        issued_at=now_utc - timedelta(hours=1),
        status=NotificationStatus.PENDING,
        attempts=0,
        next_attempt_at=now_utc,
    )
    async with uow as u:
        await u.notifications.create_if_not_exists(notif)
        await u.commit()

    sender = LoggingSender(always_fail=True)
    dispatcher = DispatchNotifications(uow, sender, clock, max_attempts=3)

    # Intento 1
    await dispatcher.run()
    async with session_factory() as session:
        n = await session.get(AlertNotification, notif_id)
        assert n is not None
        assert n.status == NotificationStatus.PENDING
        clock.set_time(n.next_attempt_at + timedelta(seconds=1))

    # Intento 2
    await dispatcher.run()
    async with session_factory() as session:
        n = await session.get(AlertNotification, notif_id)
        assert n is not None
        assert n.status == NotificationStatus.PENDING
        clock.set_time(n.next_attempt_at + timedelta(seconds=1))

    # Intento 3 (llega a max_attempts=3) -> FAILED
    r3 = await dispatcher.run()
    assert r3.failed == 1

    async with session_factory() as session:
        n = await session.get(AlertNotification, notif_id)
        assert n is not None
        assert n.status == NotificationStatus.FAILED
        assert n.attempts == 3
        assert "503 Service Unavailable" in (n.last_error or "")


async def test_concurrent_dispatchers_never_duplicate(
    session_factory: async_sessionmaker[AsyncSession], seeded_entities: dict[str, uuid.UUID]
) -> None:
    """Dos dispatchers en paralelo con SKIP LOCKED no procesan la misma fila."""
    entities = seeded_entities
    now_utc = datetime(2026, 5, 10, 12, 0, tzinfo=UTC)
    clock = FrozenClock(now_utc)
    target_date = date(2026, 5, 11)

    uow1 = SqlAlchemyUnitOfWork(session_factory)
    uow2 = SqlAlchemyUnitOfWork(session_factory)

    # Crear 10 notificaciones pendientes
    async with uow1 as u:
        for ep in range(1, 11):
            await u.notifications.create_if_not_exists(
                AlertNotification(
                    id=uuid.uuid4(),
                    alert_id=entities["alert_id"],
                    forecast_date=target_date + timedelta(days=ep),
                    episode=1,
                    event_type=WeatherEvent.FROST,
                    probability=Decimal("80.00"),
                    threshold=Decimal("60.00"),
                    issued_at=now_utc - timedelta(hours=1),
                    status=NotificationStatus.PENDING,
                    next_attempt_at=now_utc,
                )
            )
        await u.commit()

    sender = LoggingSender()
    d1 = DispatchNotifications(uow1, sender, clock, batch_size=10)
    d2 = DispatchNotifications(uow2, sender, clock, batch_size=10)

    # Disparar ambos dispatchers al mismo tiempo
    r1, r2 = await asyncio.gather(d1.run(), d2.run())

    # La suma de claimed debe ser exactamente 10 (sin solapamiento)
    assert r1.claimed + r2.claimed == 10
    assert r1.sent + r2.sent == 10
