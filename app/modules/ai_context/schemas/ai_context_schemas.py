"""Request/response DTOs for ``/notebooks/{id}/ai-context``.

The context item shape reuses the canonical :class:`LlmContextCell` from the LLM
module so the persisted/returned context drops straight into ``/llm/generate``
with no reshaping (one source of truth for ``{kind, source}``).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.core.config import settings
from app.modules.llm.schemas.llm_schemas import LlmContextCell


class AiContextStoreRequest(BaseModel):
    """Body for ``PUT /notebooks/{id}/ai-context`` — the freshly built context.

    This carries the *raw* history the front-end assembled; the server rolls it
    up to the generation budget before storing (see the AI-context service).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    context: list[LlmContextCell] = Field(default_factory=list)
    # Optional running tally of how many cell histories have been folded so far;
    # defaults to the number of items in this payload when absent.
    history_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_size(self) -> "AiContextStoreRequest":
        """Reject stored history over the byte cap (docs/ai-architecture §4.3)."""
        cap = settings.llm_max_prompt_bytes
        total_bytes = sum(len(cell.source.encode("utf-8")) for cell in self.context)
        if total_bytes > cap:
            raise ValueError(f"context exceeds the {cap} byte stored-history limit")
        return self


class AiContextResponse(BaseModel):
    """Persisted, budget-fit AI context for a notebook."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    notebook_id: UUID
    # Rolled up to fit the generation budget (≤ 8 KiB, ≤ 10 items); ready to send
    # as ``/llm/generate`` ``context``.
    context: list[LlmContextCell] = Field(default_factory=list)
    summary: str = ""
    history_count: int = 0
    updated_at: int | None = None  # unix ms; null when never stored
