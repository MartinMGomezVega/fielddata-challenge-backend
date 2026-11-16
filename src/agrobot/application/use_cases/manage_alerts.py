"""Caso de uso: Gestión de reglas de alertas (CRUD, validación y concurrencia)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from agrobot.application.etag import parse_etag
from agrobot.application.ports import Clock, UnitOfWork
from agrobot.domain.entities import Alert, IdempotencyRecord
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.errors import (
    AlertNotFound,
    DuplicateAlert,
    FieldNotFound,
    IdempotencyConflict,
    InvalidThreshold,
    VersionConflict,
)


@dataclass(frozen=True)
class CreateAlertDTO:
    user_id: uuid.UUID
    field_id: uuid.UUID
    event_type: WeatherEvent
    threshold: Decimal
    idempotency_key: str | None = None
    payload_hash: str | None = None
    response_data_factory: Callable[[Alert], tuple[int, dict[str, str], dict[str, Any]]] | None = (
        None
    )


@dataclass(frozen=True)
class CreateAlertResult:
    alert: Alert
    is_cached: bool = False
    cached_status_code: int = 201
    cached_headers: dict[str, str] | None = None
    cached_body: dict[str, Any] | None = None

    @property
    def id(self) -> uuid.UUID:
        return self.alert.id

    @property
    def field_id(self) -> uuid.UUID:
        return self.alert.field_id

    @property
    def user_id(self) -> uuid.UUID:
        return self.alert.user_id

    @property
    def event_type(self) -> WeatherEvent:
        return self.alert.event_type

    @property
    def threshold(self) -> Decimal:
        return self.alert.threshold

    @property
    def status(self) -> AlertStatus:
        return self.alert.status

    @property
    def version(self) -> int:
        return self.alert.version

    @property
    def deleted_at(self) -> Any:
        return self.alert.deleted_at

    @property
    def created_at(self) -> Any:
        return self.alert.created_at

    @property
    def updated_at(self) -> Any:
        return self.alert.updated_at


@dataclass(frozen=True)
class UpdateAlertDTO:
    alert_id: uuid.UUID
    user_id: uuid.UUID
    threshold: Decimal | None = None
    status: AlertStatus | None = None
    if_match: str | None = None


def _make_cached_result(existing: IdempotencyRecord, user_id: uuid.UUID) -> CreateAlertResult:
    cached_body_dict = json.loads(existing.response_body)
    cached_alert = Alert(
        id=uuid.UUID(cached_body_dict["id"]),
        field_id=uuid.UUID(cached_body_dict["field_id"]),
        user_id=user_id,
        event_type=WeatherEvent(cached_body_dict["event_type"]),
        threshold=Decimal(str(cached_body_dict["threshold"])),
        status=AlertStatus(cached_body_dict["status"]),
        version=1,
    )
    return CreateAlertResult(
        alert=cached_alert,
        is_cached=True,
        cached_status_code=existing.response_code,
        cached_headers=json.loads(existing.response_headers),
        cached_body=cached_body_dict,
    )


class ManageAlerts:
    """Servicio de aplicación para operaciones sobre alertas."""

    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def create_alert(self, dto: CreateAlertDTO) -> CreateAlertResult:
        """Crea una nueva alerta para un campo del usuario de forma atómica.

        Valida:
        1. 0 < threshold <= 100
        2. El campo existe y pertenece al usuario (RF-02 -> 404)
        3. No existe otra regla viva idéntica (RF-03 -> 409)
        4. Idempotencia: si se provee Idempotency-Key, guarda y verifica en la misma transacción.
        """
        if dto.threshold <= Decimal("0.00") or dto.threshold > Decimal("100.00"):
            raise InvalidThreshold(
                f"El umbral {dto.threshold} debe ser mayor a 0 y menor o igual a 100"
            )

        now = self._clock.now_utc()

        async with self._uow as uow:
            # 1. Chequeo de idempotencia si la clave está presente
            if dto.idempotency_key and dto.payload_hash:
                existing = await uow.idempotency.get(dto.user_id, dto.idempotency_key)
                if existing is not None and existing.expires_at > now:
                    if existing.request_hash != dto.payload_hash:
                        raise IdempotencyConflict(
                            "Misma Idempotency-Key pero con un payload diferente (422)"
                        )
                    return _make_cached_result(existing, dto.user_id)

            # 2. Validación de campo y pertenencia
            field = await uow.fields.get_by_id(dto.field_id, user_id=dto.user_id)
            if field is None:
                raise FieldNotFound(f"Campo {dto.field_id} no encontrado o no pertenece al usuario")

            # 3. Validación de regla duplicada activa
            is_duplicate = await uow.alerts.exists_active(
                field_id=dto.field_id,
                event_type=dto.event_type,
                threshold=dto.threshold,
            )
            if is_duplicate:
                if dto.idempotency_key and dto.payload_hash:
                    existing = await uow.idempotency.get(dto.user_id, dto.idempotency_key)
                    if existing is not None and existing.expires_at > now:
                        if existing.request_hash != dto.payload_hash:
                            raise IdempotencyConflict(
                                "Misma Idempotency-Key pero con un payload diferente (422)"
                            )
                        return _make_cached_result(existing, dto.user_id)
                raise DuplicateAlert(
                    f"Ya existe una alerta activa para el campo {dto.field_id}, "
                    f"evento {dto.event_type} y umbral {dto.threshold}"
                )

            # 4. Creación de la alerta
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
            try:
                created = await uow.alerts.create(alert)
            except DuplicateAlert as err:
                if dto.idempotency_key and dto.payload_hash:
                    await uow.rollback()
                    existing = await uow.idempotency.get(dto.user_id, dto.idempotency_key)
                    if existing is not None and existing.expires_at > now:
                        if existing.request_hash != dto.payload_hash:
                            raise IdempotencyConflict(
                                "Misma Idempotency-Key pero con un payload diferente (422)"
                            ) from err
                        return _make_cached_result(existing, dto.user_id)
                raise

            # 5. Guardar clave de idempotencia en la MISMA transacción
            if dto.idempotency_key and dto.payload_hash and dto.response_data_factory:
                status_code, headers, body = dto.response_data_factory(created)
                expires_at = now + timedelta(hours=24)
                key_record = IdempotencyRecord(
                    id=uuid.uuid4(),
                    user_id=dto.user_id,
                    key=dto.idempotency_key,
                    request_hash=dto.payload_hash,
                    response_code=status_code,
                    response_headers=json.dumps(headers),
                    response_body=json.dumps(body),
                    created_at=now,
                    expires_at=expires_at,
                )
                try:
                    await uow.idempotency.save(key_record)
                except Exception as err:
                    # Si chocó por carrera concurrente con la misma clave:
                    await uow.rollback()
                    existing = await uow.idempotency.get(dto.user_id, dto.idempotency_key)
                    if existing is not None and existing.expires_at > now:
                        if existing.request_hash != dto.payload_hash:
                            raise IdempotencyConflict(
                                "Misma Idempotency-Key pero con un payload diferente (422)"
                            ) from err
                        return _make_cached_result(existing, dto.user_id)
                    raise

            await uow.commit()
            return CreateAlertResult(alert=created, is_cached=False)

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
                if expected_version is None or alert.version != expected_version:
                    raise VersionConflict(
                        "Versión de alerta desactualizada o encabezado If-Match malformado: "
                        f"'{dto.if_match}'"
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
        """Elimina lógicamente una alerta y pasa sus notificaciones pendientes a failed."""
        async with self._uow as uow:
            alert = await uow.alerts.get_by_id(alert_id, user_id)
            if alert is None or alert.deleted_at is not None:
                return  # DELETE idempotente (204)

            now = self._clock.now_utc()
            alert.deleted_at = now
            alert.updated_at = now
            await uow.alerts.soft_delete(alert)

            # En la misma transacción: cancelar notificaciones pendientes asociadas
            await uow.notifications.mark_alert_notifications_failed(alert_id, "alert_deleted")

            await uow.commit()
