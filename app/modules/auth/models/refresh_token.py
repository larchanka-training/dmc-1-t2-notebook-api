"""ORM model for opaque refresh-token history."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RefreshToken(Base):
    """SQLAlchemy mapping for ``users.refresh_tokens``.

    The token value itself is opaque and stored only as a hash. ``family_id``
    links all rotated descendants so reuse detection can revoke the family.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("refresh_tokens_session_idx", "session_id"),
        Index("refresh_tokens_family_idx", "family_id", "created_at"),
        Index(
            "refresh_tokens_active_idx",
            "session_id",
            "expires_at",
            postgresql_where=text("revoked_at IS NULL AND rotated_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL AND rotated_at IS NULL"),
        ),
        {"schema": "users"},
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
        default=uuid4,
    )
    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        ForeignKey("users.sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reuse_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
