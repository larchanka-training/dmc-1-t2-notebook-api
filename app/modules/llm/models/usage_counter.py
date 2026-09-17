"""ORM model for LLM quota usage counters."""

from datetime import date

from sqlalchemy import BigInteger, Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LlmUsageCounter(Base):
    """SQLAlchemy mapping for ``users.llm_usage_counter``.

    Windowed aggregate counters for quota enforcement across
    scopes (user, global) and windows (day, month).
    """

    __tablename__ = "llm_usage_counter"
    __table_args__ = {"schema": "users"}

    scope: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, primary_key=True)
    window_kind: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[date] = mapped_column(Date, primary_key=True)

    calls_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_settled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_reserved_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
