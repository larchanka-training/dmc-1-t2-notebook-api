"""ORM model for the ``app.users`` table.

Минимальный пользователь: UUID, email и опциональное отображаемое имя.
До настоящего OTP/JWT мы хранили только то, без чего не работают
owner-scoped запросы к notebooks.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Uuid
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    """SQLAlchemy mapping for ``app.users``.

    «Тонкая» модель: только то, что нужно для placeholder-auth и для
    связи 1:N с notebook'ами. ``id`` — UUID (генерируется на стороне
    клиента), ``email`` уникален (constraint в Liquibase).

    Note:
        Под Postgres используется нативный ``uuid``-тип; под SQLite
        в тестах ``Uuid(as_uuid=True)`` подменяется generic-вариантом.
    """

    __tablename__ = "users"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
    )
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
