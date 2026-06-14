"""Data-access layer for the ``NotebookAiContext`` row.

Thin SQLAlchemy wrapper, no business logic. Like the notebooks repository it
``flush``es into the open transaction but never ``commit``s — that is
:func:`app.core.db.get_db`'s responsibility.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.ai_context.models.ai_context import NotebookAiContext


class AiContextRepository:
    """Repository for ``notebooks.notebook_ai_context`` rows."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, notebook_id: UUID) -> NotebookAiContext | None:
        """Return the stored context row for a notebook, or ``None``."""
        return self.db.get(NotebookAiContext, notebook_id)

    def upsert(
        self,
        *,
        notebook_id: UUID,
        owner_id: UUID,
        context: list[dict],
        summary: str,
        history_count: int,
        updated_at: datetime,
    ) -> NotebookAiContext:
        """Insert or update the single context row for a notebook."""
        row = self.db.get(NotebookAiContext, notebook_id)
        if row is None:
            row = NotebookAiContext(notebook_id=notebook_id)
        row.owner_id = owner_id
        row.context = context
        row.summary = summary
        row.history_count = history_count
        row.updated_at = updated_at
        self.db.add(row)
        self.db.flush()
        return row

    def delete(self, notebook_id: UUID) -> None:
        """Remove the stored context row if present (rebuild-on-delete clear)."""
        row = self.db.get(NotebookAiContext, notebook_id)
        if row is not None:
            self.db.delete(row)
            self.db.flush()
