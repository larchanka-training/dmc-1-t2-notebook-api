"""ORM model for the append-only LLM usage event ledger."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LlmUsageEvent(Base):
    """SQLAlchemy mapping for ``users.llm_usage_event``.

    Append-only ledger of provider calls (guard, generator, repair)
    serving as durable evidence of consumption and actual costs.
    """

    __tablename__ = "llm_usage_event"
    __table_args__ = (
        Index("llm_usage_event_user_created_idx", "user_id", "created_at"),
        Index("llm_usage_event_request_idx", "request_id"),
        Index("llm_usage_event_reservation_idx", "reservation_id"),
        {"schema": "users"},
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        ForeignKey("users.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        nullable=False,
    )
    reservation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        nullable=False,
    )
    call_kind: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
