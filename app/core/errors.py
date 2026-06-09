"""Unified error envelope and FastAPI exception handlers.

Все ошибки API возвращаются в едином виде::

    {"error": {"code": str, "message": str, "fields": dict}}

Здесь определены Pydantic-схемы этого «конверта» и три обработчика
(валидация, ``HTTPException``, generic ``Exception``), которые
устанавливаются на приложение через :func:`install_error_handlers`.
Generic-обработчик намеренно не отдаёт ``str(exc)`` наружу, чтобы не
утекали внутренние детали (см. Шаг 6 разбора PR #29).
"""

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import Request


class ApiError(BaseModel):
    """Payload of a single API error.

    Содержимое поля ``error`` в ответе. Используется FE для
    локализации и анализа: код (``code``) стабилен и пригоден для
    ``switch``-логики, ``message`` — текст для пользователя/лога,
    ``fields`` — пер-полевые ошибки валидации.
    """

    code: str
    message: str
    fields: dict[str, str] = Field(default_factory=dict)


class ApiErrorResponse(BaseModel):
    """Top-level error envelope returned by the API.

    Внешняя «обёртка», чтобы у любого ответа об ошибке был один и тот
    же ключ верхнего уровня. Эта схема также прокидывается в OpenAPI
    как ``responses`` для роутов, чтобы FE-генератор типов знал форму.
    """

    error: ApiError


def error_response(
    status_code: int,
    code: str,
    message: str,
    fields: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a ``JSONResponse`` shaped as the standard error envelope.

    Хелпер, чтобы во всех обработчиках и сервисах конструировать ответ
    одинаково. Если в роуте нужно вернуть «свою» ошибку, проще бросить
    ``HTTPException(detail={"code": ..., "message": ...})``, а
    обработчик внизу превратит её сюда.

    Args:
        status_code: HTTP-статус ответа.
        code: Машиночитаемый код ошибки (``UPPER_SNAKE_CASE``).
        message: Человекочитаемое сообщение.
        fields: Карта «имя поля → причина», для валидационных ошибок.

    Returns:
        Готовый ``JSONResponse`` для возврата из обработчика.
    """
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
    """Translate a Pydantic ``loc`` tuple to a dotted field path.

    Pydantic возвращает позицию ошибки как ``("body", "cells", 3, "id")``.
    Для клиентов удобнее видеть ``"cells[3].id"`` — этим занят хелпер.
    Префикс ``body/query/path/header`` отбрасывается как технический.

    Args:
        loc: Кортеж локации из ``ValidationError``.

    Returns:
        Строка вида ``"cells[3].id"``.
    """
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
    """Register the standard set of error handlers on a FastAPI app.

    Подключает три обработчика, в порядке приоритета: валидация запросов
    (422), штатные ``HTTPException`` (любой статус, заданный роутом),
    и любые непойманные ``Exception`` (500). Вызывается один раз в
    :mod:`app.main` при сборке приложения.

    Args:
        app: Инстанс FastAPI, к которому подключаются обработчики.
    """

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Turn Pydantic ``RequestValidationError`` into a 422 envelope.

        Собирает все ошибки валидации в карту ``{field: message}`` и
        возвращает стандартный конверт ``VALIDATION_ERROR``.
        """
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
        """Convert ``HTTPException`` into the standard error envelope.

        Если в ``detail`` пришёл словарь со схемой
        ``{"code", "message", "fields"}`` — переиспользуем поля; иначе
        используем общий код ``HTTP_ERROR`` и текст ``detail``.
        """
        code = "HTTP_ERROR"
        message = str(exc.detail)
        fields: dict[str, str] = {}

        if isinstance(exc.detail, dict):
            code = str(exc.detail.get("code", code))
            message = str(exc.detail.get("message", message))
            raw_fields = exc.detail.get("fields")
            if isinstance(raw_fields, dict):
                fields = {str(key): str(value) for key, value in raw_fields.items()}

        response = error_response(exc.status_code, code, message, fields)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Catch any other exception and return a safe 500 envelope.

        Намеренно не пробрасывает ``str(exc)`` в ответ — иначе наружу
        может уехать строка SQL, путь файла или другой внутренний
        контекст (Шаг 6 разбора PR #29).
        """
        return error_response(
            500,
            "INTERNAL_SERVER_ERROR",
            "Internal server error",
        )
