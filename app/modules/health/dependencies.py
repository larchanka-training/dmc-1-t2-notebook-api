"""FastAPI dependencies for the health module.

Здесь живут DI-фабрики, которые знают, **как именно** собрать
readiness-результат из storage-зависимостей. Контроллеры импортируют
только готовый :func:`get_readiness` и не должны знать про
:class:`Session` или :func:`get_db` (см. ``api/docs/domain-boundaries.md``
§5 — общее правило «controllers без SQLAlchemy»).
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.modules.health.schemas import HealthResponse
from app.modules.health.services import build_readiness


def get_readiness(db: Session = Depends(get_db)) -> HealthResponse:
    """Build the readiness payload using the request-scoped DB session.

    Args:
        db: Сессия из :func:`get_db`.

    Returns:
        :class:`HealthResponse` с компонентами и общим статусом.
    """
    return build_readiness(db)
