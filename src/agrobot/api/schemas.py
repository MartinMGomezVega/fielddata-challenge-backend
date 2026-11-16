"""Esquemas Pydantic v2 para la API REST.

Espejo fiel de docs/openapi.yaml.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from agrobot.domain.enums import AlertStatus, NotificationStatus, WeatherEvent

PercentageNumber = Annotated[
    Decimal,
    PlainSerializer(lambda x: float(x), return_type=float, when_used="json"),
]


class ProblemDetail(BaseModel):
    """Detalle de error RFC 9457 (application/problem+json)."""

    type: str = Field(
        default="https://agrobot.dev/problems/error",
        json_schema_extra={"example": "https://agrobot.dev/problems/alert-conflict"},
    )
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[ValidationErrorItem] | None = None


class ValidationErrorItem(BaseModel):
    loc: list[str]
    msg: str


class AlertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: uuid.UUID
    event_type: WeatherEvent
    threshold: Decimal = Field(
        gt=Decimal("0.00"),
        le=Decimal("100.00"),
        description="Notificar cuando probability >= threshold.",
    )


class AlertUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: Decimal | None = Field(
        default=None,
        gt=Decimal("0.00"),
        le=Decimal("100.00"),
    )
    status: AlertStatus | None = None

    @model_validator(mode="after")
    def validate_min_properties(self) -> AlertUpdate:
        if self.threshold is None and self.status is None:
            raise ValueError("Al menos un campo (threshold o status) debe ser provisto")
        return self


class AlertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    field_id: uuid.UUID
    event_type: WeatherEvent
    threshold: PercentageNumber
    status: AlertStatus
    created_at: datetime
    updated_at: datetime


class PageMetaResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class AlertPage(BaseModel):
    items: list[AlertResponse]
    meta: PageMetaResponse


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alert_id: uuid.UUID
    forecast_date: date
    episode: int = Field(ge=1)
    event_type: WeatherEvent
    probability: PercentageNumber
    threshold: PercentageNumber
    issued_at: datetime
    status: NotificationStatus
    attempts: int
    created_at: datetime
    sent_at: datetime | None = None


class NotificationPage(BaseModel):
    items: list[NotificationResponse]
    meta: PageMetaResponse


class ForecastResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    forecast_date: date
    event_type: WeatherEvent
    probability: PercentageNumber
    issued_at: datetime
    ingested_at: datetime


COMMON_RESPONSES = {
    404: {"model": ProblemDetail, "description": "No existe o no pertenece al usuario"},
    409: {"model": ProblemDetail, "description": "Conflicto"},
    412: {"model": ProblemDetail, "description": "Precondition Failed (If-Match desactualizado)"},
    422: {"model": ProblemDetail, "description": "Error de validación"},
}
