from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.health.schemas import ComponentStatus, HealthResponse

logger = get_logger(__name__)


def check_database(db: Session) -> ComponentStatus:
    try:
        db.execute(text("SELECT 1"))
        return ComponentStatus(name="database", status="ok")
    except SQLAlchemyError as exc:
        logger.warning("health.database.fail", error=str(exc))
        return ComponentStatus(name="database", status="fail", detail=str(exc))


def build_liveness() -> HealthResponse:
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        components=[],
    )


def build_readiness(db: Session) -> HealthResponse:
    components = [check_database(db)]
    overall = "ok" if all(c.status == "ok" for c in components) else "degraded"
    return HealthResponse(
        status=overall,
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        components=components,
    )
