"""Router para gestión de alertas (/api/v1/alerts)."""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from fastapi.responses import JSONResponse

from agrobot.api.deps import get_current_user_id, get_manage_alerts
from agrobot.api.schemas import (
    COMMON_RESPONSES,
    AlertCreate,
    AlertPage,
    AlertResponse,
    AlertUpdate,
)
from agrobot.application.etag import format_etag
from agrobot.application.use_cases.manage_alerts import (
    CreateAlertDTO,
    ManageAlerts,
    UpdateAlertDTO,
)
from agrobot.domain.entities import Alert
from agrobot.domain.enums import AlertStatus, WeatherEvent

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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    body_json = body.model_dump_json()
    body_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()

    def response_factory(alert: Alert) -> tuple[int, dict[str, str], dict[str, Any]]:
        etag = format_etag(alert.version)
        location = f"/api/v1/alerts/{alert.id}"
        response_data = AlertResponse.model_validate(alert).model_dump(mode="json")
        return status.HTTP_201_CREATED, {"ETag": etag, "Location": location}, response_data

    dto = CreateAlertDTO(
        user_id=user_id,
        field_id=body.field_id,
        event_type=body.event_type,
        threshold=body.threshold,
        idempotency_key=idempotency_key,
        payload_hash=body_hash if idempotency_key else None,
        response_data_factory=response_factory if idempotency_key else None,
    )
    result = await service.create_alert(dto)
    if result.is_cached:
        return JSONResponse(
            status_code=result.cached_status_code,
            content=result.cached_body,
            headers=result.cached_headers,
        )

    assert result.alert is not None
    etag = format_etag(result.alert.version)
    location = f"/api/v1/alerts/{result.alert.id}"
    response_data = AlertResponse.model_validate(result.alert).model_dump(mode="json")
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
    status: Annotated[AlertStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> AlertPage:
    alerts, next_cursor = await service.list_alerts(
        user_id=user_id,
        field_id=field_id,
        event_type=event_type,
        status=status,
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
