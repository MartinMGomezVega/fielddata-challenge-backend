"""Manejo de errores global RFC 9457 (application/problem+json)."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agrobot.domain.errors import (
    AlertNotFound,
    DuplicateAlert,
    FieldNotFound,
    IdempotencyConflict,
    InvalidThreshold,
    VersionConflict,
)


def make_problem_response(
    status_code: int,
    problem_type: str,
    title: str,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, object]] | None = None,
) -> JSONResponse:
    content: dict[str, object] = {
        "type": problem_type,
        "title": title,
        "status": status_code,
    }
    if detail:
        content["detail"] = detail
    if instance:
        content["instance"] = instance
    if errors:
        content["errors"] = errors

    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AlertNotFound)
    async def alert_not_found_handler(request: Request, exc: AlertNotFound) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            problem_type="https://agrobot.dev/problems/not-found",
            title="Not Found",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(FieldNotFound)
    async def field_not_found_handler(request: Request, exc: FieldNotFound) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            problem_type="https://agrobot.dev/problems/not-found",
            title="Not Found",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(DuplicateAlert)
    async def duplicate_alert_handler(request: Request, exc: DuplicateAlert) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_409_CONFLICT,
            problem_type="https://agrobot.dev/problems/alert-conflict",
            title="Conflict",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(VersionConflict)
    async def version_conflict_handler(request: Request, exc: VersionConflict) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            problem_type="https://agrobot.dev/problems/precondition-failed",
            title="Precondition Failed",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(InvalidThreshold)
    async def invalid_threshold_handler(request: Request, exc: InvalidThreshold) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            problem_type="https://agrobot.dev/problems/validation-error",
            title="Validation Error",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(
        request: Request, exc: IdempotencyConflict
    ) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            problem_type="https://agrobot.dev/problems/validation-error",
            title="Validation Error",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"loc": [str(loc_item) for loc_item in err.get("loc", [])], "msg": err.get("msg", "")}
            for err in exc.errors()
        ]
        return make_problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            problem_type="https://agrobot.dev/problems/validation-error",
            title="Validation Error",
            detail="Request body or parameters validation failed",
            instance=str(request.url.path),
            errors=errors,
        )
