"""ORM model for in-flight LLM usage reservations."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LlmUsageReservation(Base):
    """SQLAlchemy mapping for ``users.llm_usage_reservation``.

    Durable per-call reservation tracking in-flight provider invocations
    and state transitions (reserved -> started -> settled / released / unknown).
    """

    __tablename__ = "llm_usage_reservation"
    __table_args__ = (
        Index("llm_usage_reservation_state_created_idx", "state", "created_at"),
        Index("llm_usage_reservation_request_idx", "request_id"),
        Index("llm_usage_reservation_user_idx", "user_id"),
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
    call_kind: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    cost_reserved_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
