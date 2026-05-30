"""FastAPI dependencies for the notebooks module.

Здесь живут DI-фабрики, которые знают, **как именно** собрать сервис
ноутбуков из конкретной storage-реализации. Контроллеры импортируют
только готовый :func:`get_notebook_service` и не должны знать про
:class:`Session`, :class:`NotebookRepository` или другие
storage-specific детали (см. ``api/docs/domain-boundaries.md`` §5–6).

Когда notebooks-домен переедет на NoSQL, DI выберет другую реализацию
репозитория. :class:`NotebookService` продолжит работать через
storage-neutral :class:`NotebookEntity` и :class:`NotebookRepositoryProtocol`.
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.services.notebook_service import NotebookService


def get_notebook_service(db: Session = Depends(get_db)) -> NotebookService:
    """Provide a request-scoped :class:`NotebookService`.

    Сборка цепочки ``Session → NotebookRepository → NotebookService``.
    Каждый запрос получает свой инстанс сервиса, привязанный к своей
    сессии; в тестах подменяется через ``app.dependency_overrides``.

    Args:
        db: Сессия из :func:`get_db`.

    Returns:
        Готовый к работе :class:`NotebookService`.
    """
    return NotebookService(NotebookRepository(db))
