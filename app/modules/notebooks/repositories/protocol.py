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

from app.modules.notebooks.models.notebook import Notebook


class NotebookRepositoryProtocol(Protocol):
    """Methods every Notebook repository implementation must provide.

    На MVP контракт возвращает ORM-объект :class:`Notebook` напрямую.
    Когда дойдём до NoSQL-реализации — стоит ввести «доменную сущность»
    (обычный :class:`dataclasses.dataclass` без SQLAlchemy), и протокол
    начнёт работать с ней; сервис от этого не сломается, так как уже
    не знает про ORM напрямую.
    """

    def get_by_id(self, notebook_id: UUID) -> Notebook | None:
        """Fetch a notebook by primary key (including soft-deleted)."""
        ...

    def list_by_owner(
        self,
        owner_id: UUID,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> tuple[list[Notebook], int]:
        """Return a page of active notebooks for ``owner_id`` plus total count."""
        ...

    def save(self, notebook: Notebook) -> Notebook:
        """Persist a new or modified notebook within the open transaction."""
        ...

    def soft_delete(self, notebook: Notebook, deleted_at: datetime) -> Notebook:
        """Mark a notebook as deleted by setting ``deleted_at``."""
        ...
