"""RetellAI-compatible error envelope + exception handlers (feature 003).

The compat sub-app emits RetellAI's ``{"status": <int>, "message": <str>}`` body on EVERY
error path, isolated from the native ``/v1`` ``{"detail": ...}`` shape. Starlette does not
share exception handlers across a mount, so these are registered on the compat sub-app
ONLY (FR-004 / SC-007). The catch-all never leaks a traceback or PHI (Constitution II/VI):
it logs the exception type-name and returns a fixed ``internal error`` message.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException


class CompatError(Exception):
    """A RetellAI-shaped error raised by compat handlers/services.

    Carries the HTTP status + the RetellAI ``message`` string; the handler renders it as
    ``{"status": status_code, "message": message}`` with the matching HTTP status code.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _envelope(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"status": status_code, "message": message}
    )


async def _handle_compat_error(_request: Request, exc: Exception) -> JSONResponse:
    status_code = exc.status_code if isinstance(exc, CompatError) else 500
    message = exc.message if isinstance(exc, CompatError) else "internal error"
    return _envelope(status_code, message)


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    # FastAPI/Starlette HTTPExceptions (unmatched route -> 404, a Depends 401) rendered in
    # the RetellAI envelope instead of the native {"detail": ...}.
    if isinstance(exc, StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        return _envelope(exc.status_code, detail)
    return _envelope(500, "internal error")


async def _handle_validation_error(_request: Request, exc: Exception) -> JSONResponse:
    # Request validation failure -> 422 naming only the first offending field. The full
    # error list is never echoed: it can contain submitted values (potential PHI).
    message = "invalid request"
    if isinstance(exc, RequestValidationError) and exc.errors():
        loc = exc.errors()[0].get("loc") or ()
        field = ".".join(str(p) for p in loc[1:])
        if field:
            message = f"invalid request: {field}"
    return _envelope(422, message)


async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
    # Last-resort guard: log the type name ONLY (never the message/traceback/PHI) and
    # return a fixed envelope so a bug can never leak internals to the CRM (Constitution VI).
    logger.bind(exc_type=type(exc).__name__).error("Unhandled compat error: {exc_type}")
    return _envelope(500, "internal error")


def register_exception_handlers(app: FastAPI) -> None:
    """Register the four compat exception handlers on the mounted sub-app."""
    app.add_exception_handler(CompatError, _handle_compat_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected)
