"""Storage contract for the Notebook aggregate.

«Контракт» хранилища ноутбуков. :class:`NotebookService` типизируется
против этого протокола, а не против конкретной SQL-реализации, чтобы
при переезде notebooks-домена на NoSQL (см. ``api/docs/domain-boundaries.md``)
можно было подменить :class:`NotebookRepository` на новую реализацию
без правок в сервисе и контроллерах.

Любой класс, у которого есть методы с такой сигнатурой, считается
реализацией протокола (PEP 544 / structural subtyping). Наследование
от :class:`Protocol` явно не требуется.
"""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.notebooks.entities import NotebookEntity


class NotebookRepositoryProtocol(Protocol):
    """Methods every Notebook repository implementation must provide.

    Контракт возвращает :class:`NotebookEntity`, а не ORM-модель. Поэтому
    сервис не зависит от SQLAlchemy, а конкретный репозиторий сам решает,
    как материализовать entity в SQL row, Mongo document или другое
    storage-представление.
    """

    def get_by_id(self, notebook_id: UUID) -> NotebookEntity | None:
        """Fetch a notebook by primary key (including soft-deleted)."""
        ...

    def list_by_owner(
        self,
        owner_id: UUID,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> tuple[list[NotebookEntity], int]:
        """Return a page of active notebooks for ``owner_id`` plus total count."""
        ...

    def save(self, notebook: NotebookEntity) -> NotebookEntity:
        """Persist a new or modified notebook within the open transaction."""
        ...

    def soft_delete(
        self, notebook: NotebookEntity, deleted_at: datetime
    ) -> NotebookEntity:
        """Mark a notebook as deleted by setting ``deleted_at``."""
        ...
