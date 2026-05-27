"""ORM model for the ``app.notebooks`` table.

Содержит метаданные ноутбука и его ячейки в JSONB-поле ``cells``.
Ячейки умышленно денормализованы (один документ — один ноутбук): это
упрощает sync с фронтом и убирает необходимость в join'ах.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Notebook(Base):
    """SQLAlchemy mapping for ``app.notebooks``.

    Ноутбук принадлежит одному пользователю (``owner_id``) и удаляется
    «мягко» через ``deleted_at`` (NULL = активен). ``cells`` хранит
    массив ячеек как JSONB (на Postgres) или generic JSON (на SQLite в
    тестах). Ключи внутри JSON — camelCase: контракт фиксирован с FE,
    смена потребует data-миграции и согласованного релиза.

    Attributes:
        id: UUID-ключ. Генерируется на клиенте (offline-first).
        owner_id: FK на ``app.users.id``, ``ON DELETE CASCADE``.
        title: До 255 символов (валидация и в схеме, и в БД).
        format_version: Версия формата документа; растёт с миграциями.
        cells: Список ячеек в API-формате (camelCase).
        created_at: Когда запись создана сервером.
        updated_at: «Логическое» время изменения (см. ``_compute_updated_at``).
        deleted_at: Метка soft-delete или ``None``.
    """

    __tablename__ = "notebooks"
    __table_args__ = {"schema": "app"}

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        primary_key=True,
    )
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True).with_variant(PgUUID(as_uuid=True), "postgresql"),
        ForeignKey("app.users.id", ondelete="CASCADE"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
