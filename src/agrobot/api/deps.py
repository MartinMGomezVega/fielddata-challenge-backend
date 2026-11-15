"""Inyección de dependencias (Composition Root) para FastAPI."""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from agrobot.application.ports import Clock, UnitOfWork
from agrobot.application.use_cases.manage_alerts import ManageAlerts
from agrobot.infrastructure.clock import SystemClock
from agrobot.infrastructure.db.session import get_session_factory
from agrobot.infrastructure.db.uow import SqlAlchemyUnitOfWork


def get_current_user_id(
    x_user_id: str = Header(alias="X-User-Id", description="UUID del usuario autenticado"),
) -> uuid.UUID:
    """Extrae y valida el identificador de usuario desde el header X-User-Id."""
    try:
        return uuid.UUID(x_user_id)
    except ValueError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Formato de X-User-Id inválido: {x_user_id}",
        ) from err


def get_clock() -> Clock:
    return SystemClock()


def get_uow() -> UnitOfWork:
    factory = get_session_factory()
    return SqlAlchemyUnitOfWork(factory)


def get_manage_alerts(
    uow: Annotated[UnitOfWork, Depends(get_uow)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> ManageAlerts:
    return ManageAlerts(uow, clock)
