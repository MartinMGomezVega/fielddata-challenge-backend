"""Aplicación principal FastAPI y configuración del ciclo de vida."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agrobot.api.errors import register_error_handlers
from agrobot.api.routers import alerts, forecasts, health, notifications
from agrobot.config import settings
from agrobot.infrastructure.db.session import dispose_engine
from agrobot.logging import configure_logging, get_logger

logger = get_logger("api.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    _ = app
    configure_logging(settings.log_level)
    logger.info("api_startup", environment=settings.environment)
    yield
    logger.info("api_shutdown_disposing_engine")
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agrobot - Climate Alerts API",
        version="1.0.0",
        description=(
            "API para configurar alertas climáticas sobre campos y consultar las "
            "notificaciones generadas por el evaluador en background."
        ),
        lifespan=lifespan,
    )

    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(alerts.router)
    app.include_router(notifications.router)
    app.include_router(forecasts.router)

    return app


app = create_app()
