from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.modules.notebooks.models.notebook import Notebook

SORT_COLUMNS = {
    "updatedAt": Notebook.updated_at,
    "createdAt": Notebook.created_at,
    "title": Notebook.title,
}


class NotebookRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_id(self, notebook_id: UUID) -> Notebook | None:
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
        self.db.add(notebook)
        self.db.flush()  # пушим SQL в БД, но без commit
        return notebook

    def soft_delete(self, notebook: Notebook, deleted_at: datetime) -> Notebook:
        notebook.deleted_at = deleted_at
        self.db.add(notebook)
        self.db.flush()
        return notebook
