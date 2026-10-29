"""Response envelope: {"ok": true, "data": ...} / {"ok": false, "error": {code, message, detail}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from hero.logging import get_logger

STATUS_BY_CODE = {
    "not_found": 404,
    "validation_error": 422,
    "unauthorized": 401,
    "run_in_progress": 409,
    "budget_exhausted": 402,
    "upstream_error": 502,
    "internal_error": 500,
}
CODE_BY_STATUS = {status: code for code, status in STATUS_BY_CODE.items()}


def ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status_code)


def fail(code: str, message: str, detail: Any = None, status_code: int | None = None) -> JSONResponse:
    status = status_code or STATUS_BY_CODE.get(code, 500)
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message, "detail": detail}},
        status_code=status,
    )


class ApiError(Exception):
    """Raise anywhere in a handler; the exception handler turns it into the envelope."""

    def __init__(self, code: str, message: str, detail: Any = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code or STATUS_BY_CODE.get(code, 500)


def register_exception_handlers(app: FastAPI) -> None:
    log = get_logger("hero.api")

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return fail(exc.code, exc.message, exc.detail, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return fail("validation_error", "The request did not pass validation.", exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = CODE_BY_STATUS.get(exc.status_code, "internal_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if exc.status_code == 404 and not message:
            message = "Not found."
        return fail(code, message, None, exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.error("http.unhandled", path=request.url.path, error=repr(exc))
        return fail("internal_error", "Something went wrong on the server.", None, 500)


__all__ = ["ok", "fail", "ApiError", "HTTPException", "register_exception_handlers", "STATUS_BY_CODE"]
