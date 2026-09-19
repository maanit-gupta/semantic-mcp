"""The single error envelope: {"error": {"code", "message", "details"}} with a non-200 status.

Every failure path (our own ApiError, FastAPI validation, Starlette routing errors, unexpected exceptions) is
converted here, so no handler can drift into a different shape. Validation errors keep only the location and message:
caller input is never echoed back.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AsciiJSONResponse(JSONResponse):
    # ensure_ascii=True: a lone surrogate from caller input ("\ud800") is escaped instead of making UTF-8
    # encoding of the response fail with a 500.
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details = status_code, code, message, details or {}


def error_response(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return AsciiJSONResponse(body, status_code=status_code)


_HTTP_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}


def _loc(parts: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in parts)


def install_error_handlers(app: FastAPI) -> None:
    # Each handler records its code on request.state so the audit middleware can log it without reading the body.

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        request.state.error_code = exc.code
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request.state.error_code = "validation_error"
        errors = [{"loc": _loc(err["loc"]), "message": err["msg"].removeprefix("Value error, ")} for err in exc.errors()]
        return error_response(422, "validation_error", "request is invalid", {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        request.state.error_code = code
        message = "no such route" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, __: Exception) -> JSONResponse:
        return error_response(500, "internal_error", "internal error")
