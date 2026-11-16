"""Tests unitarios para ManageAlerts usando fakes en memoria."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agrobot.application.ports import (
    AlertRepository,
    BreachStateRepository,
    Clock,
    FieldRepository,
    ForecastRepository,
    IdempotencyRepository,
    NotificationRepository,
    UnitOfWork,
)
from agrobot.application.use_cases.manage_alerts import (
    CreateAlertDTO,
    ManageAlerts,
    UpdateAlertDTO,
)
from agrobot.domain.entities import Alert, Field
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.errors import (
    AlertNotFound,
    DuplicateAlert,
    FieldNotFound,
    InvalidThreshold,
    VersionConflict,
)
from agrobot.infrastructure.clock import FrozenClock


class FakeNotificationRepository:
    def __init__(self) -> None:
        self.marked_failed: list[tuple[uuid.UUID, str]] = []

    async def mark_alert_notifications_failed(self, alert_id: uuid.UUID, reason: str) -> int:
        self.marked_failed.append((alert_id, reason))
        return 0


class FakeAlertRepository:
    def __init__(self) -> None:
        self.alerts: dict[uuid.UUID, Alert] = {}

    async def create(self, alert: Alert) -> Alert:
        self.alerts[alert.id] = alert
        return alert

    async def get_by_id(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> Alert | None:
        alert = self.alerts.get(alert_id)
        if alert and alert.user_id == user_id:
            return alert
        return None

    async def get_by_id_system(self, alert_id: uuid.UUID) -> Alert | None:
        return self.alerts.get(alert_id)

    async def find_active_rules(
        self, batch_size: int, after_id: uuid.UUID | None = None
    ) -> list[Alert]:
        active = [
            a
            for a in self.alerts.values()
            if a.status == AlertStatus.ACTIVE and a.deleted_at is None
        ]
        active.sort(key=lambda a: a.id)
        if after_id:
            active = [a for a in active if a.id > after_id]
        return active[:batch_size]

    async def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        field_id: uuid.UUID | None = None,
        event_type: WeatherEvent | None = None,
        status: AlertStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Alert], str | None]:
        _ = cursor
        user_alerts = [
            a for a in self.alerts.values() if a.user_id == user_id and a.deleted_at is None
        ]
        if field_id:
            user_alerts = [a for a in user_alerts if a.field_id == field_id]
        if event_type:
            user_alerts = [a for a in user_alerts if a.event_type == event_type]
        if status:
            user_alerts = [a for a in user_alerts if a.status == status]
        user_alerts.sort(key=lambda a: (a.created_at, a.id), reverse=True)
        return user_alerts[:limit], None

    async def update(self, alert: Alert) -> Alert:
        self.alerts[alert.id] = alert
        return alert

    async def soft_delete(self, alert: Alert) -> None:
        self.alerts[alert.id] = alert

    async def exists_active(
        self,
        field_id: uuid.UUID,
        event_type: WeatherEvent,
        threshold: Decimal,
        exclude_id: uuid.UUID | None = None,
    ) -> bool:
        for a in self.alerts.values():
            if exclude_id and a.id == exclude_id:
                continue
            if (
                a.field_id == field_id
                and a.event_type == event_type
                and a.threshold == threshold
                and a.deleted_at is None
            ):
                return True
        return False


class FakeFieldRepository:
    def __init__(self) -> None:
        self.fields: dict[uuid.UUID, Field] = {}

    async def get_by_id(
        self, field_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> Field | None:
        field = self.fields.get(field_id)
        if field and user_id is not None and field.owner_id != user_id:
            return None
        return field


class FakeUnitOfWork(UnitOfWork):
    def __init__(self, alert_repo: FakeAlertRepository, field_repo: FakeFieldRepository) -> None:
        self.alerts: AlertRepository = alert_repo
        self.fields: FieldRepository = field_repo
        self.forecasts: ForecastRepository = None  # type: ignore[assignment]
        self.breach_states: BreachStateRepository = None  # type: ignore[assignment]
        self.notifications: NotificationRepository = FakeNotificationRepository()  # type: ignore[assignment]
        self.idempotency: IdempotencyRepository = None  # type: ignore[assignment]

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


@pytest.fixture
def fake_context() -> tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock]:
    alert_repo = FakeAlertRepository()
    field_repo = FakeFieldRepository()
    clock = FrozenClock(datetime(2026, 5, 10, 12, 0, tzinfo=UTC))
    uow = FakeUnitOfWork(alert_repo, field_repo)
    service = ManageAlerts(uow, clock)
    return service, alert_repo, field_repo, clock


async def test_create_alert_success(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    dto = CreateAlertDTO(
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.FROST,
        threshold=Decimal("60.00"),
    )
    alert = await service.create_alert(dto)
    assert alert.field_id == field_id
    assert alert.user_id == user_id
    assert alert.threshold == Decimal("60.00")
    assert alert.status == AlertStatus.ACTIVE
    assert alert.version == 1


async def test_create_alert_foreign_field_raises_404(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=other_user_id,  # Campo ajeno
        name="Lote Ajeno",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    dto = CreateAlertDTO(
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.FROST,
        threshold=Decimal("60.00"),
    )
    with pytest.raises(FieldNotFound):
        await service.create_alert(dto)


async def test_create_alert_duplicate_rule_raises_conflict(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    dto = CreateAlertDTO(
        user_id=user_id,
        field_id=field_id,
        event_type=WeatherEvent.RAIN,
        threshold=Decimal("70.00"),
    )
    await service.create_alert(dto)

    with pytest.raises(DuplicateAlert):
        await service.create_alert(dto)


async def test_create_alert_invalid_threshold_raises_error(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    # Umbral 0
    with pytest.raises(InvalidThreshold):
        await service.create_alert(
            CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal("0.00"))
        )

    # Umbral 101
    with pytest.raises(InvalidThreshold):
        await service.create_alert(
            CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal("101.00"))
        )


async def test_update_alert_with_if_match_and_optimistic_locking(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    created = await service.create_alert(
        CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal("50.00"))
    )
    assert created.version == 1

    # Desactualizado If-Match (espera 2 pero está en 1) -> VersionConflict
    with pytest.raises(VersionConflict):
        await service.update_alert(
            UpdateAlertDTO(created.id, user_id, threshold=Decimal("55.00"), if_match='W/"2"')
        )

    # Coincide If-Match -> éxito e incrementa versión a 2
    updated = await service.update_alert(
        UpdateAlertDTO(created.id, user_id, threshold=Decimal("55.00"), if_match='W/"1"')
    )
    assert updated.threshold == Decimal("55.00")
    assert updated.version == 2


async def test_delete_alert_idempotent(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    alert = await service.create_alert(
        CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal("60.00"))
    )

    # Primer borrado: éxito
    await service.delete_alert(alert.id, user_id)

    # La alerta queda como borrada
    with pytest.raises(AlertNotFound):
        await service.get_alert(alert.id, user_id)

    # Segundo borrado idempotente: no falla
    await service.delete_alert(alert.id, user_id)


async def test_list_alerts_and_cursor_pagination(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, clock = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    for i in range(5):
        await service.create_alert(
            CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal(f"{50 + i}.00"))
        )

    alerts, cursor = await service.list_alerts(user_id, limit=3)
    assert len(alerts) == 3


async def test_update_alert_status(
    fake_context: tuple[ManageAlerts, FakeAlertRepository, FakeFieldRepository, Clock],
) -> None:
    service, _, field_repo, _ = fake_context
    user_id = uuid.uuid4()
    field_id = uuid.uuid4()
    field = Field(
        id=field_id,
        owner_id=user_id,
        name="Lote 1",
        latitude=Decimal("-31.4"),
        longitude=Decimal("-64.1"),
        timezone="America/Argentina/Cordoba",
    )
    field_repo.fields[field_id] = field

    alert = await service.create_alert(
        CreateAlertDTO(user_id, field_id, WeatherEvent.FROST, Decimal("60.00"))
    )
    updated = await service.update_alert(
        UpdateAlertDTO(alert.id, user_id, status=AlertStatus.PAUSED)
    )
    assert updated.status == AlertStatus.PAUSED
