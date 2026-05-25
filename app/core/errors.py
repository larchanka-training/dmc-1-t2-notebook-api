from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request


class ApiError(BaseModel):
    code: str
    message: str
    fields: dict[str, str] = Field(default_factory=dict)


class ApiErrorResponse(BaseModel):
    error: ApiError


def error_response(
    status_code: int,
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "fields": fields or {},
            }
        },
    )


def _loc_to_field(loc: tuple[Any, ...]) -> str:
    parts = list(loc)
    if parts and parts[0] in {"body", "query", "path", "header"}:
        parts = parts[1:]

    field = ""
    for part in parts:
        if isinstance(part, int):
            field += f"[{part}]"
        else:
            field = f"{field}.{part}" if field else str(part)
    return field


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        fields = {
            _loc_to_field(tuple(error["loc"])): str(error["msg"])
            for error in exc.errors()
        }
        return error_response(
            422,
            "VALIDATION_ERROR",
            "Request validation failed",
            fields,
        )

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        message = str(exc.detail)
        fields: dict[str, str] = {}

        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("code", code))
            message = str(exc.detail.get("message", message))
            raw_fields = exc.detail.get("fields")
            if isinstance(raw_fields, dict):
                fields = {str(key): str(value) for key, value in raw_fields.items()}

        return error_response(exc.status_code, code, message, fields)
