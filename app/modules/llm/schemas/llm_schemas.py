"""Pydantic schemas for the Cloud LLM generation endpoint, usage views, and reconciliation."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from app.core.config import settings

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

MAX_PROMPT_LENGTH = 8_000
MAX_CONTEXT_SOURCE_LENGTH = 8_000
MAX_BASE_CODE_LENGTH = 8_000
# Single source of truth for the generation context item ceiling: the request
# field cap below AND the summary roll-up (ai_context) both use this.
MAX_CONTEXT_ITEMS = 10

# Context-item kinds. ``code``/``markdown``/``text`` are verbatim neighbour-cell
# source. ``output`` (a truncated cell output), ``globals`` (a compact
# name/type/shape digest of the runtime global scope) and ``summary`` (the
# budget-aware roll-up of older history, docs/ai-architecture.md §4.3) carry a
# pre-formatted compact string in ``source`` and share the same byte budget, so
# the size validator below applies to them unchanged.
ContextCellKind = Literal["code", "markdown", "text", "output", "globals", "summary"]
ResultKind = Literal["code", "text"]


class LlmContextCell(BaseModel):
    """Neighboring notebook cell (or digest) sent as generation context."""

    kind: ContextCellKind
    source: str = Field(..., max_length=MAX_CONTEXT_SOURCE_LENGTH)


class GenerateRequest(BaseModel):
    """Request body for ``POST /llm/generate``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_LENGTH)
    mode: Literal["generate", "edit"] = "generate"
    language: Literal["javascript", "typescript"] = "javascript"
    notebook_title: str | None = Field(default=None, max_length=200)
    context: list[LlmContextCell] = Field(
        default_factory=list, max_length=MAX_CONTEXT_ITEMS
    )
    base_code: str | None = Field(default=None, max_length=MAX_BASE_CODE_LENGTH)

    @model_validator(mode="after")
    def validate_mode_payload(self) -> "GenerateRequest":
        """Validate cross-field and byte-size constraints."""
        if self.mode == "edit" and not (self.base_code or "").strip():
            raise ValueError("baseCode is required when mode is edit")

        prompt_cap_kib = settings.llm_max_prompt_bytes // 1024
        if len(self.prompt.encode("utf-8")) > settings.llm_max_prompt_bytes:
            raise ValueError(
                f"prompt exceeds the {prompt_cap_kib} KiB UTF-8 byte limit"
            )

        context_bytes = sum(len(cell.source.encode("utf-8")) for cell in self.context)
        if context_bytes > settings.llm_max_prompt_bytes:
            raise ValueError(
                f"context exceeds the {prompt_cap_kib} KiB UTF-8 byte limit"
            )
        return self


class TokenUsage(BaseModel):
    """Token usage metadata returned by the provider."""

    prompt: int = 0
    completion: int = 0


class GenerateResponse(BaseModel):
    """Successful LLM generation response."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    result_kind: ResultKind = "code"
    content: str
    model: str
    tier: Literal["backend"] = "backend"
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    request_id: UUID


class LlmQuotaWindowView(BaseModel):
    """Aggregated quota and usage view for a scope and time window."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    scope: Literal["user", "global"]
    window_kind: Literal["day", "month"]
    window_start: date
    calls_reserved: int
    calls_settled: int
    calls_total: int
    call_limit: int | None = None
    cost_reserved_micros: int
    cost_micros: int
    cost_total_micros: int
    cost_limit_micros: int | None = None
    resets_at: datetime
    retry_after: int


class LlmUserUsageResponse(BaseModel):
    """User-facing usage and quota view for daily and monthly windows."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_id: UUID
    tier: str
    day: LlmQuotaWindowView
    month: LlmQuotaWindowView


class LlmReservationSummary(BaseModel):
    """Summary of an LLM usage reservation for admin and debug inspection."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    user_id: UUID
    request_id: UUID
    call_kind: str
    provider: str
    state: str
    cost_reserved_micros: int
    created_at: datetime
    started_at: datetime | None = None
    closed_at: datetime | None = None


class LlmEventSummary(BaseModel):
    """Summary of an LLM usage ledger event for admin and debug inspection."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    user_id: UUID
    request_id: UUID
    reservation_id: UUID
    call_kind: str
    provider: str
    model_id: str | None = None
    status: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_micros: int
    created_at: datetime


class LlmAdminUsageResponse(BaseModel):
    """Admin and debug usage inspection view."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    global_day: LlmQuotaWindowView
    global_month: LlmQuotaWindowView
    recent_reservations: list[LlmReservationSummary]
    recent_events: list[LlmEventSummary]


class LlmReconciliationSummary(BaseModel):
    """Result summary of a stale reservation reconciliation run."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    reconciled_reserved: int
    reconciled_started: int
    returned_cost_micros: int
    cutoff: datetime
