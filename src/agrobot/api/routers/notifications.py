"""Router para consulta de notificaciones (/api/v1/notifications)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from agrobot.api.deps import get_current_user_id, get_uow
from agrobot.api.schemas import (
    COMMON_RESPONSES,
    NotificationPage,
    NotificationResponse,
    PageMetaResponse,
)
from agrobot.application.pagination import PageParams
from agrobot.application.ports import UnitOfWork
from agrobot.domain.enums import NotificationStatus
from agrobot.domain.errors import AlertNotFound

router = APIRouter(tags=["Notifications"])


@router.get(
    "/api/v1/alerts/{alert_id}/notifications",
    response_model=NotificationPage,
    operation_id="listAlertNotifications",
    summary="Historial de notificaciones de una alerta",
    responses={
        200: {"model": NotificationPage, "description": "OK"},
        404: COMMON_RESPONSES[404],
    },
)
async def list_alert_notifications(
    alert_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    page: Annotated[int, Query(ge=1, description="Número de página (empieza en 1)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Elementos por página")] = 10,
) -> NotificationPage:
    params = PageParams(page=page, page_size=page_size)
    async with uow as u:
        alert = await u.alerts.get_by_id_include_deleted(alert_id, user_id)
        if alert is None:
            raise AlertNotFound(f"Alerta {alert_id} no encontrada")

        page_result = await u.notifications.get_by_alert(
            alert_id=alert_id,
            user_id=user_id,
            params=params,
        )
        items = [NotificationResponse.model_validate(n) for n in page_result.items]
        return NotificationPage(
            items=items,
            meta=PageMetaResponse(**dataclasses.asdict(page_result.meta)),
        )


@router.get(
    "/api/v1/notifications",
    response_model=NotificationPage,
    operation_id="listNotifications",
    summary="Mis notificaciones (bandeja)",
)
async def list_notifications(
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    status: NotificationStatus | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    page: Annotated[int, Query(ge=1, description="Número de página (empieza en 1)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Elementos por página")] = 10,
) -> NotificationPage:
    params = PageParams(page=page, page_size=page_size)
    async with uow as u:
        page_result = await u.notifications.list_by_user(
            user_id=user_id,
            status=status,
            from_date=from_date,
            to_date=to_date,
            params=params,
        )
        items = [NotificationResponse.model_validate(n) for n in page_result.items]
        return NotificationPage(
            items=items,
            meta=PageMetaResponse(**dataclasses.asdict(page_result.meta)),
        )
