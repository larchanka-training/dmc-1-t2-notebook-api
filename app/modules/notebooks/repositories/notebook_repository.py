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

from app.modules.notebooks.entities import NotebookEntity
from app.modules.notebooks.models.notebook import Notebook

#: Карта «имя сортировки из API → колонка в ORM». Используется для
#: безопасного перевода клиентского ``sort`` в реальный ``ORDER BY``.
SORT_COLUMNS = {
    "updatedAt": Notebook.updated_at,
    "createdAt": Notebook.created_at,
    "title": Notebook.title,
}


class NotebookRepository:
    """Repository for ``notebooks.notebooks`` rows.

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

    def get_by_id(self, notebook_id: UUID) -> NotebookEntity | None:
        """Fetch a notebook by primary key (including soft-deleted ones).

        Сервис сам решает, считать ли запись «живой» по ``deleted_at``;
        репозиторий не фильтрует — это даёт сервису возможность отдавать
        корректный 404/409 для разных сценариев.

        Args:
            notebook_id: UUID ноутбука.

        Returns:
            ``NotebookEntity`` или ``None``.
        """
        statement = select(Notebook).where(Notebook.id == notebook_id)
        row = self.db.execute(statement).scalar_one_or_none()
        return self._to_entity(row) if row is not None else None

    def list_by_owner(
        self,
        owner_id: UUID,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> tuple[list[NotebookEntity], int]:
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
        items = [self._to_entity(row) for row in self.db.execute(statement).scalars()]
        return items, total

    def save(self, notebook: NotebookEntity) -> NotebookEntity:
        """Persist a new or modified notebook within the open transaction.

        SQLAlchemy-материализация спрятана внутри репозитория: сервис
        передаёт storage-neutral entity, а репозиторий создаёт или
        обновляет ORM row.

        Args:
            notebook: Entity для сохранения.

        Returns:
            Сохранённая entity.
        """
        row = self.db.get(Notebook, notebook.id)
        if row is None:
            row = self._to_model(notebook)
        else:
            self._apply_entity(row, notebook)

        self.db.add(row)
        self.db.flush()  # пушим SQL в БД, но без commit
        return self._to_entity(row)

    def soft_delete(
        self, notebook: NotebookEntity, deleted_at: datetime
    ) -> NotebookEntity:
        """Mark a notebook as deleted by setting ``deleted_at``.

        Soft-delete — это не ``DELETE``, а ``UPDATE``. Запись остаётся
        в таблице, но всё, что фильтрует по ``deleted_at IS NULL``,
        перестаёт её видеть.

        Args:
            notebook: ORM-объект, который нужно «погасить».
            deleted_at: Метка времени удаления (обычно ``now()``).

        Returns:
            Entity с проставленным ``deleted_at``.
        """
        deleted = NotebookEntity(
            id=notebook.id,
            owner_id=notebook.owner_id,
            title=notebook.title,
            format_version=notebook.format_version,
            cells=notebook.cells,
            created_at=notebook.created_at,
            updated_at=notebook.updated_at,
            deleted_at=deleted_at,
        )
        return self.save(deleted)

    def _to_entity(self, row: Notebook) -> NotebookEntity:
        """Map a SQLAlchemy row to the storage-neutral domain entity."""
        return NotebookEntity(
            id=row.id,
            owner_id=row.owner_id,
            title=row.title,
            format_version=row.format_version,
            cells=row.cells or [],
            created_at=row.created_at,
            updated_at=row.updated_at,
            deleted_at=row.deleted_at,
        )

    def _to_model(self, entity: NotebookEntity) -> Notebook:
        """Map a domain entity to a new SQLAlchemy row."""
        return Notebook(
            id=entity.id,
            owner_id=entity.owner_id,
            title=entity.title,
            format_version=entity.format_version,
            cells=entity.cells,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            deleted_at=entity.deleted_at,
        )

    def _apply_entity(self, row: Notebook, entity: NotebookEntity) -> None:
        """Apply domain entity state to an existing SQLAlchemy row."""
        row.owner_id = entity.owner_id
        row.title = entity.title
        row.format_version = entity.format_version
        row.cells = entity.cells
        row.created_at = entity.created_at
        row.updated_at = entity.updated_at
        row.deleted_at = entity.deleted_at
