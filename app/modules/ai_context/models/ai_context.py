"""ORM model for the ``notebooks.notebook_ai_context`` table.

Stores the per-notebook AI generation context built on the front-end and rolled
up server-side (docs/ai-architecture.md §4.3). One row per notebook: the
budget-fit ``context`` items (ready to drop into ``/llm/generate``), the
``summary`` roll-up string, and a ``history_count`` of how many raw cell
histories fed the last build.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, Integer, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NotebookAiContext(Base):
    """SQLAlchemy mapping for ``notebooks.notebook_ai_context``.

    Lives in the ``notebooks`` schema as a sub-resource of a notebook, keyed by
    ``notebook_id``. No DB-level FK to ``notebooks.notebooks`` — the notebooks
    domain is kept free of DB coupling so it can move to a NoSQL store later
    (api/docs/domain-boundaries.md §4); ownership/existence is enforced in the
    service via the notebooks service.

    Attributes:
        notebook_id: UUID of the owning notebook (primary key).
        owner_id: Denormalised owner UUID (defence-in-depth for owner scoping).
        context: Budget-fit context items (camelCase ``{kind, source}``), already
            rolled up to fit the generation byte/slot budget.
        summary: The roll-up digest string ("" when no roll-up was needed).
        history_count: How many raw cell histories fed the last build.
        updated_at: When this row was last written.
    """

    __tablename__ = "notebook_ai_context"
    __table_args__ = {"schema": "notebooks"}

    notebook_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
    )
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        nullable=False,
        index=True,
    )
    context: Mapped[list[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    history_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
