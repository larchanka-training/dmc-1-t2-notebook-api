"""Probe builders: liveness and readiness payloads.

Чистая логика, без HTTP-слоя: контроллер просто вызывает эти функции
и оборачивает результат. Это удобно тестировать в изоляции.
"""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.health.schemas import ComponentStatus, HealthResponse

logger = get_logger(__name__)


def check_database(db: Session) -> ComponentStatus:
    """Ping the database with ``SELECT 1`` and report status.

    Лёгкий ping. ``SELECT 1`` не нагружает БД и не зависит от
    наличия таблиц — это «жив ли движок и работает ли соединение».
    Любая ошибка SA конвертируется в ``status="fail"`` с детализацией
    для логов (наружу `detail` отдаём осторожно).

    Args:
        db: SQLAlchemy-сессия.

    Returns:
        :class:`ComponentStatus` с ``status`` ``"ok"`` или ``"fail"``.
    """
    try:
        db.execute(text("SELECT 1"))
        return ComponentStatus(name="database", status="ok")
    except SQLAlchemyError as exc:
        logger.warning("health.database.fail", error=str(exc))
        return ComponentStatus(name="database", status="fail", detail=str(exc))


def build_liveness() -> HealthResponse:
    """Return a static ``HealthResponse`` describing the process.

    Liveness-проверка отдаёт «процесс жив», ничего не вызывая по сети.
    Это значит: даже при недоступной БД оркестратор не считает под
    «мёртвым» и не убивает его — это и есть смысл liveness vs readiness.

    Returns:
        :class:`HealthResponse` со статусом ``"ok"``.
    """
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        components=[],
    )


def build_readiness(db: Session) -> HealthResponse:
    """Return a ``HealthResponse`` aggregating dependency probes.

    Readiness — «готов ли принимать трафик». Если хоть один компонент
    не ``ok``, общий статус становится ``degraded`` и контроллер
    конвертирует это в HTTP 503.

    Args:
        db: SQLAlchemy-сессия из :func:`get_db`.

    Returns:
        :class:`HealthResponse` с заполненным списком ``components``.
    """
    components = [check_database(db)]
    overall = "ok" if all(c.status == "ok" for c in components) else "degraded"
    return HealthResponse(
        status=overall,
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        components=components,
    )
