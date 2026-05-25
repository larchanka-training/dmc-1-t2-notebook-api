from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Notebook(Base):
    __tablename__ = "notebooks"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
    )
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        ForeignKey("app.users.id"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Cells are stored in API-shaped camelCase per the FE/BE contract.
    # Changing this requires a data migration and coordinated FE update.
    cells: Mapped[list[dict]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
