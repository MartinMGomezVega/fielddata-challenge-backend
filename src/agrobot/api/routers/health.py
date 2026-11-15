"""Router para verificaciones de salud (/health/live y /health/ready)."""

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from agrobot.infrastructure.db.session import get_session_factory
from agrobot.logging import get_logger

logger = get_logger("health")

router = APIRouter(tags=["Health"])


@router.get(
    "/health/live",
    status_code=status.HTTP_200_OK,
    operation_id="liveness",
    summary="El proceso responde",
)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/health/ready",
    status_code=status.HTTP_200_OK,
    operation_id="readiness",
    summary="Puede atender tráfico (DB alcanzable)",
    responses={
        200: {"description": "OK"},
        503: {"description": "Dependencia caída"},
    },
)
async def readiness(response: Response) -> dict[str, str]:
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        logger.error("readiness_check_failed", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "error": "Database unreachable"}
