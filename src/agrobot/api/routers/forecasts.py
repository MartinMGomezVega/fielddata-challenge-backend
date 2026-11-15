"""Router para consulta de pronósticos climáticos (/api/v1/fields/{field_id}/forecasts)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends

from agrobot.api.deps import get_current_user_id, get_uow
from agrobot.api.schemas import COMMON_RESPONSES, ForecastResponse
from agrobot.application.ports import UnitOfWork
from agrobot.domain.enums import WeatherEvent
from agrobot.domain.errors import FieldNotFound

router = APIRouter(tags=["Forecasts"])


@router.get(
    "/api/v1/fields/{field_id}/forecasts",
    response_model=list[ForecastResponse],
    operation_id="listFieldForecasts",
    summary="Pronóstico almacenado para un campo (sólo lectura, soporte/debug)",
    responses={
        200: {"description": "OK"},
        404: COMMON_RESPONSES[404],
    },
)
async def list_field_forecasts(
    field_id: uuid.UUID,
    user_id: Annotated[uuid.UUID, Depends(get_current_user_id)],
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    from_date: date | None = None,
    to_date: date | None = None,
    event_type: WeatherEvent | None = None,
) -> list[ForecastResponse]:
    async with uow as u:
        field = await u.fields.get_by_id(field_id, user_id=user_id)
        if field is None:
            raise FieldNotFound(f"Campo {field_id} no encontrado o no pertenece al usuario")

        forecasts = await u.forecasts.get_for_field(
            field_id=field_id,
            from_date=from_date,
            to_date=to_date,
            event_type=event_type,
        )
        return [ForecastResponse.model_validate(f) for f in forecasts]
