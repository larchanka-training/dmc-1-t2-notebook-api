"""Data-access layer for ``Notebook`` aggregate.

Тонкая обёртка над SQLAlchemy, без бизнес-логики. Все методы работают
в рамках единой сессии (``self.db``) и не вызывают ``commit`` — за это
отвечает :func:`get_db` (см. Шаг 12 разбора PR #29). Здесь только
``flush``, чтобы пушнуть SQL в текущую транзакцию.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.modules.notebooks.models.notebook import Notebook

#: Карта «имя сортировки из API → колонка в ORM». Используется для
#: безопасного перевода клиентского ``sort`` в реальный ``ORDER BY``.
SORT_COLUMNS = {
    "updatedAt": Notebook.updated_at,
    "createdAt": Notebook.created_at,
    "title": Notebook.title,
}


class NotebookRepository:
    """Repository for ``app.notebooks`` rows.

    Принимает request-scoped ``Session`` и оперирует через неё. Все
    публичные методы — это «единицы работы»: один вызов = одно
    логическое действие (``select``/``insert``/``update``).
    """

    def __init__(self, db: Session) -> None:
        """Bind the repository to a SQLAlchemy session.

        Args:
            db: Сессия из :func:`get_db`. Транзакция — её обязанность.
        """
        self.db = db

    def get_by_id(self, notebook_id: UUID) -> Notebook | None:
        """Fetch a notebook by primary key (including soft-deleted ones).

        Сервис сам решает, считать ли запись «живой» по ``deleted_at``;
        репозиторий не фильтрует — это даёт сервису возможность отдавать
        корректный 404/409 для разных сценариев.

        Args:
            notebook_id: UUID ноутбука.

        Returns:
            ``Notebook`` или ``None``.
        """
        statement = select(Notebook).where(Notebook.id == notebook_id)
        return self.db.execute(statement).scalar_one_or_none()

    def list_by_owner(
        self,
        owner_id: UUID,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> tuple[list[Notebook], int]:
        """List active notebooks for an owner with pagination and sorting.

        Возвращает «страницу» ноутбуков и общее число записей. Жёстко
        фильтрует по ``owner_id`` и ``deleted_at IS NULL`` — это и есть
        owner-scoping для всего модуля. ``id ASC`` добавлен в
        ``ORDER BY`` как стабильный tie-breaker для пагинации.

        Args:
            owner_id: UUID владельца.
            limit: Размер страницы.
            offset: Смещение от начала.
            sort: Ключ сортировки (из ``ALLOWED_SORTS``).
            order: Направление (``"asc"`` или ``"desc"``).

        Returns:
            Пара ``(items, total)``.
        """
        column = SORT_COLUMNS[sort]
        ordering = column.asc() if order == "asc" else column.desc()
        filters = (Notebook.owner_id == owner_id, Notebook.deleted_at.is_(None))

        total_statement = select(func.count()).select_from(Notebook).where(*filters)
        total = int(self.db.execute(total_statement).scalar_one())

        statement: Select[tuple[Notebook]] = (
            select(Notebook)
            .where(*filters)
            .order_by(ordering, Notebook.id.asc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self.db.execute(statement).scalars().all())
        return items, total

    def save(self, notebook: Notebook) -> Notebook:
        """Persist a new or modified notebook within the open transaction.

        ``add`` + ``flush``: SQL отправляется в БД, но транзакция не
        закрывается — её закроет :func:`get_db` после успешного роута.
        Используется и для INSERT, и для UPDATE: SQLAlchemy сам решает,
        что это, по состоянию объекта.

        Args:
            notebook: ORM-объект для сохранения.

        Returns:
            Тот же ``notebook`` (для удобной chain-нотации в сервисе).
        """
        self.db.add(notebook)
        self.db.flush()  # пушим SQL в БД, но без commit
        return notebook

    def soft_delete(self, notebook: Notebook, deleted_at: datetime) -> Notebook:
        """Mark a notebook as deleted by setting ``deleted_at``.

        Soft-delete — это не ``DELETE``, а ``UPDATE``. Запись остаётся
        в таблице, но всё, что фильтрует по ``deleted_at IS NULL``,
        перестаёт её видеть.

        Args:
            notebook: ORM-объект, который нужно «погасить».
            deleted_at: Метка времени удаления (обычно ``now()``).

        Returns:
            Тот же ``notebook`` с проставленным ``deleted_at``.
        """
        notebook.deleted_at = deleted_at
        self.db.add(notebook)
        self.db.flush()
        return notebook
