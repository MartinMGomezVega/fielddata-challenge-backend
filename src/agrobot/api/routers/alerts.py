"""Router para gestión de alertas (/api/v1/alerts)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from fastapi.responses import JSONResponse

from agrobot.api.deps import get_clock, get_current_user_id, get_manage_alerts, get_uow
from agrobot.api.schemas import (
    COMMON_RESPONSES,
    AlertCreate,
    AlertPage,
    AlertResponse,
    AlertUpdate,
)
from agrobot.application.ports import Clock, UnitOfWork
from agrobot.application.use_cases.manage_alerts import (
    CreateAlertDTO,
    ManageAlerts,
    UpdateAlertDTO,
    format_etag,
)
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.domain.errors import IdempotencyConflict
from agrobot.infrastructure.db.models import IdempotencyKey

router = APIRouter(prefix="/api/v1/alerts", tags=["Alerts"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=AlertResponse,
    operation_id="createAlert",
    summary="Crear una alerta sobre un campo",
    responses={
        201: {"model": AlertResponse, "description": "Creada"},
        404: COMMON_RESPONSES[404],
        409: COMMON_RESPONSES[409],
        422: COMMON_RESPONSES[422],
    },
)
async def create_alert(
    body: AlertCreate,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    service: Annotated[ManageAlerts, Depends(get_manage_alerts)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Clock, Depends(get_clock)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    body_json = body.model_dump_json()
    body_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()

    # Manejo de Idempotency-Key si está presente
    if idempotency_key is not None:
        async with uow as u:
            existing = await u.idempotency.get(user_id, idempotency_key)
            if existing is not None:
                if existing.request_hash != body_hash:
                    raise IdempotencyConflict(
                        "Misma Idempotency-Key pero con un payload diferente (422)"
                    )
                cached_headers: dict[str, str] = json.loads(existing.response_headers)
                return JSONResponse(
                    status_code=existing.response_code,
                    content=json.loads(existing.response_body),
                    headers=cached_headers,
                )

    dto = CreateAlertDTO(
        user_id=user_id,
        field_id=body.field_id,
        event_type=body.event_type,
        threshold=body.threshold,
    )
    alert = await service.create_alert(dto)
    etag = format_etag(alert.version)
    location = f"/api/v1/alerts/{alert.id}"

    response_data = AlertResponse.model_validate(alert).model_dump(mode="json")

    # Guardar llave de idempotencia con TTL de 24h
    if idempotency_key is not None:
        async with uow as u:
            expires_at = clock.now_utc() + timedelta(hours=24)
            key_record = IdempotencyKey(
                id=uuid.uuid4(),
                user_id=user_id,
                key=idempotency_key,
                request_hash=body_hash,
                response_code=status.HTTP_201_CREATED,
                response_headers=json.dumps({"ETag": etag, "Location": location}),
                response_body=json.dumps(response_data),
                expires_at=expires_at,
            )
            await u.idempotency.save(key_record)
            await u.commit()

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=response_data,
        headers={"ETag": etag, "Location": location},
    )


@router.get(
    "",
    response_model=AlertPage,
    operation_id="listAlerts",
    summary="Listar mis alertas (paginado por cursor)",
)
async def list_alerts(
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    service: Annotated[ManageAlerts, Depends(get_manage_alerts)],
    field_id: uuid.UUID | None = None,
    event_type: WeatherEvent | None = None,
    status_filter: AlertStatus | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> AlertPage:
    alerts, next_cursor = await service.list_alerts(
        user_id=user_id,
        field_id=field_id,
        event_type=event_type,
        status=status_filter,
        limit=limit,
        cursor=cursor,
    )
    items = [AlertResponse.model_validate(a) for a in alerts]
    return AlertPage(items=items, next_cursor=next_cursor)


@router.get(
    "/{alert_id}",
    response_model=AlertResponse,
    operation_id="getAlert",
    summary="Obtener una alerta",
    responses={
        200: {"model": AlertResponse, "description": "OK"},
        404: COMMON_RESPONSES[404],
    },
)
async def get_alert(
    alert_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    service: Annotated[ManageAlerts, Depends(get_manage_alerts)],
) -> Response:
    alert = await service.get_alert(alert_id, user_id)
    etag = format_etag(alert.version)
    response_data = AlertResponse.model_validate(alert).model_dump(mode="json")
    return JSONResponse(content=response_data, headers={"ETag": etag})


@router.patch(
    "/{alert_id}",
    response_model=AlertResponse,
    operation_id="updateAlert",
    summary="Modificar umbral o pausar/reactivar",
    responses={
        200: {"model": AlertResponse, "description": "Actualizada"},
        404: COMMON_RESPONSES[404],
        409: COMMON_RESPONSES[409],
        412: COMMON_RESPONSES[412],
        422: COMMON_RESPONSES[422],
    },
)
async def update_alert(
    alert_id: uuid.UUID,
    body: AlertUpdate,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    service: Annotated[ManageAlerts, Depends(get_manage_alerts)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    dto = UpdateAlertDTO(
        alert_id=alert_id,
        user_id=user_id,
        threshold=body.threshold,
        status=body.status,
        if_match=if_match,
    )
    updated = await service.update_alert(dto)
    etag = format_etag(updated.version)
    response_data = AlertResponse.model_validate(updated).model_dump(mode="json")
    return JSONResponse(content=response_data, headers={"ETag": etag})


@router.delete(
    "/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteAlert",
    summary="Eliminar una alerta (borrado lógico)",
)
async def delete_alert(
    alert_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    service: Annotated[ManageAlerts, Depends(get_manage_alerts)],
) -> Response:
    await service.delete_alert(alert_id, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
