"""Manejo de errores global RFC 9457 (application/problem+json)."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agrobot.domain.errors import (
    AlertNotFound,
    DuplicateAlert,
    FieldNotFound,
    IdempotencyConflict,
    InvalidCursor,
    InvalidThreshold,
    VersionConflict,
)
from agrobot.logging import get_logger

logger = get_logger("api.errors")


def make_problem_response(
    status_code: int,
    problem_type: str,
    title: str,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[dict[str, object]] | None = None,
    headers: dict[str, str] | None = None,
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
        headers=headers,
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            problem_type="https://agrobot.dev/problems/validation-error",
            title="Validation Error",
            detail=exc.message,
            instance=str(request.url.path),
        )

    @app.exception_handler(InvalidCursor)
    async def invalid_cursor_handler(request: Request, exc: InvalidCursor) -> JSONResponse:
        return make_problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            problem_type="https://agrobot.dev/problems/validation-error",
            title="Validation Error",
            detail="Request body or parameters validation failed",
            instance=str(request.url.path),
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title_map = {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            412: "Precondition Failed",
            422: "Validation Error",
            500: "Internal Server Error",
        }
        problem_map = {
            404: "https://agrobot.dev/problems/not-found",
            409: "https://agrobot.dev/problems/alert-conflict",
            412: "https://agrobot.dev/problems/precondition-failed",
            422: "https://agrobot.dev/problems/validation-error",
        }
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        headers_dict = dict(exc.headers) if exc.headers is not None else None
        return make_problem_response(
            status_code=exc.status_code,
            problem_type=problem_map.get(exc.status_code, "https://agrobot.dev/problems/error"),
            title=title_map.get(exc.status_code, "HTTP Error"),
            detail=detail,
            instance=str(request.url.path),
            headers=headers_dict,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_internal_error", error=str(exc), path=str(request.url.path))
        return make_problem_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            problem_type="https://agrobot.dev/problems/internal-error",
            title="Internal Server Error",
            detail="An unexpected error occurred",
            instance=str(request.url.path),
        )
