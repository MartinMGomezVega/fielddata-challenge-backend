"""Caso de uso: Gestión de reglas de alertas (CRUD, validación y concurrencia)."""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from agrobot.application.ports import Clock, UnitOfWork
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.errors import (
    AlertNotFound,
    DuplicateAlert,
    FieldNotFound,
    InvalidThreshold,
    VersionConflict,
)
from agrobot.infrastructure.db.models import Alert


@dataclass(frozen=True)
class CreateAlertDTO:
    user_id: uuid.UUID
    field_id: uuid.UUID
    event_type: WeatherEvent
    threshold: Decimal


@dataclass(frozen=True)
class UpdateAlertDTO:
    alert_id: uuid.UUID
    user_id: uuid.UUID
    threshold: Decimal | None = None
    status: AlertStatus | None = None
    if_match: str | None = None


def parse_etag(etag: str | None) -> int | None:
    """Extrae la versión numérica de un header ETag (ej. 'W/"3"' o '"3"' o '3')."""
    if not etag:
        return None
    cleaned = etag.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:]
    cleaned = cleaned.strip('"')
    try:
        return int(cleaned)
    except ValueError:
        return None


def format_etag(version: int) -> str:
    """Genera el ETag débil estándar según especificación."""
    return f'W/"{version}"'


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    """Genera un cursor opaco codificado en base64 para keyset pagination."""
    raw = f"{created_at.isoformat()}_{item_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor_str: str) -> tuple[datetime, uuid.UUID] | None:
    """Decodifica un cursor opaco."""
    try:
        raw = base64.urlsafe_b64decode(cursor_str.encode()).decode()
        dt_str, id_str = raw.split("_", 1)
        return datetime.fromisoformat(dt_str), uuid.UUID(id_str)
    except Exception:
        return None


class ManageAlerts:
    """Servicio de aplicación para operaciones sobre alertas."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def create_alert(self, dto: CreateAlertDTO) -> Alert:
        """Crea una nueva alerta para un campo del usuario.

        Valida:
        1. 0 < threshold <= 100
        2. El campo existe y pertenece al usuario (RF-02 -> 404)
        3. No existe otra regla viva idéntica (RF-03 -> 409)
        """
        if dto.threshold <= Decimal("0.00") or dto.threshold > Decimal("100.00"):
            raise InvalidThreshold(
                f"El umbral {dto.threshold} debe ser mayor a 0 y menor o igual a 100"
            )

        async with self._uow as uow:
            field = await uow.fields.get_by_id(dto.field_id, user_id=dto.user_id)
            if field is None:
                raise FieldNotFound(f"Campo {dto.field_id} no encontrado o no pertenece al usuario")

            is_duplicate = await uow.alerts.exists_active(
                field_id=dto.field_id,
                event_type=dto.event_type,
                threshold=dto.threshold,
            )
            if is_duplicate:
                raise DuplicateAlert(
                    f"Ya existe una alerta activa para el campo {dto.field_id}, "
                    f"evento {dto.event_type} y umbral {dto.threshold}"
                )

            now = self._clock.now_utc()
            alert = Alert(
                id=uuid.uuid4(),
                user_id=dto.user_id,
                field_id=dto.field_id,
                event_type=dto.event_type,
                threshold=dto.threshold,
                status=AlertStatus.ACTIVE,
                created_at=now,
                updated_at=now,
                version=1,
            )
            created = await uow.alerts.create(alert)
            await uow.commit()
            return created

    async def get_alert(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> Alert:
        """Obtiene una alerta por ID validando pertenencia."""
        async with self._uow as uow:
            alert = await uow.alerts.get_by_id(alert_id, user_id)
            if alert is None or alert.deleted_at is not None:
                raise AlertNotFound(f"Alerta {alert_id} no encontrada")
            return alert

    async def list_alerts(
        self,
        user_id: uuid.UUID,
        *,
        field_id: uuid.UUID | None = None,
        event_type: WeatherEvent | None = None,
        status: AlertStatus | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Alert], str | None]:
        """Lista alertas del usuario con filtros y paginación por cursor."""
        async with self._uow as uow:
            return await uow.alerts.list_by_user(
                user_id,
                field_id=field_id,
                event_type=event_type,
                status=status,
                limit=limit,
                cursor=cursor,
            )

    async def update_alert(self, dto: UpdateAlertDTO) -> Alert:
        """Actualiza el umbral o pausa/reactiva una alerta con optimistic locking."""
        async with self._uow as uow:
            alert = await uow.alerts.get_by_id(dto.alert_id, dto.user_id)
            if alert is None or alert.deleted_at is not None:
                raise AlertNotFound(f"Alerta {dto.alert_id} no encontrada")

            # Validación de optimistic concurrency con If-Match
            if dto.if_match is not None:
                expected_version = parse_etag(dto.if_match)
                if expected_version is not None and alert.version != expected_version:
                    raise VersionConflict(
                        f"Versión de alerta desactualizada (esperada: {expected_version}, "
                        f"actual: {alert.version})"
                    )

            if dto.threshold is not None:
                if dto.threshold <= Decimal("0.00") or dto.threshold > Decimal("100.00"):
                    raise InvalidThreshold("El umbral debe ser > 0 y <= 100")

                if dto.threshold != alert.threshold:
                    # Validar unicidad de la nueva regla
                    is_duplicate = await uow.alerts.exists_active(
                        field_id=alert.field_id,
                        event_type=alert.event_type,
                        threshold=dto.threshold,
                        exclude_id=alert.id,
                    )
                    if is_duplicate:
                        raise DuplicateAlert(
                            f"Ya existe una alerta con umbral {dto.threshold} "
                            "para este campo y evento"
                        )
                    alert.threshold = dto.threshold

            if dto.status is not None:
                alert.status = dto.status

            alert.version += 1
            alert.updated_at = self._clock.now_utc()
            updated = await uow.alerts.update(alert)
            await uow.commit()
            return updated

    async def delete_alert(self, alert_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Elimina lógicamente una alerta de forma idempotente (RF-06, AC-16)."""
        async with self._uow as uow:
            alert = await uow.alerts.get_by_id(alert_id, user_id)
            if alert is None or alert.deleted_at is not None:
                # Idempotente: borrar algo ya borrado o inexistente no falla
                return

            alert.deleted_at = self._clock.now_utc()
            await uow.alerts.soft_delete(alert)
            await uow.commit()
