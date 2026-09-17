"""ORM model for user LLM entitlements and tier overrides."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LlmEntitlement(Base):
    """SQLAlchemy mapping for ``users.llm_entitlement``.

    Stores user tier classification ('free', 'developer', 'paid') and
    optional call limit overrides.
    """

    __tablename__ = "llm_entitlement"
    __table_args__ = {"schema": "users"}

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        ForeignKey("users.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tier: Mapped[str] = mapped_column(String, nullable=False)
    daily_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_call_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
