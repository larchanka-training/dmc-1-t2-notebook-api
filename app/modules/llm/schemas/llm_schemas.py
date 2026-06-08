"""Pydantic schemas for the Cloud LLM generation endpoint."""

from typing import Literal
from uuid import UUID

from app.core.config import settings

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

MAX_PROMPT_LENGTH = 8_000
MAX_CONTEXT_SOURCE_LENGTH = 8_000
MAX_BASE_CODE_LENGTH = 8_000


class LlmContextCell(BaseModel):
    """Neighboring notebook cell sent as generation context."""

    kind: Literal["code", "markdown", "text"]
    source: str = Field(..., max_length=MAX_CONTEXT_SOURCE_LENGTH)


class GenerateRequest(BaseModel):
    """Request body for ``POST /llm/generate``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_LENGTH)
    mode: Literal["generate", "edit"] = "generate"
    language: Literal["javascript", "typescript"] = "javascript"
    notebook_title: str | None = Field(default=None, max_length=200)
    context: list[LlmContextCell] = Field(default_factory=list, max_length=10)
    base_code: str | None = Field(default=None, max_length=MAX_BASE_CODE_LENGTH)

    @model_validator(mode="after")
    def validate_mode_payload(self) -> "GenerateRequest":
        """Validate cross-field and byte-size constraints."""
        if self.mode == "edit" and not (self.base_code or "").strip():
            raise ValueError("baseCode is required when mode is edit")
        if len(self.prompt.encode("utf-8")) > settings.llm_max_prompt_bytes:
            raise ValueError("prompt exceeds the 8 KiB UTF-8 byte limit")

        context_bytes = sum(len(cell.source.encode("utf-8")) for cell in self.context)
        if context_bytes > settings.llm_max_prompt_bytes:
            raise ValueError("context exceeds the 8 KiB UTF-8 byte limit")
        return self


class TokenUsage(BaseModel):
    """Token usage metadata returned by the provider."""

    prompt: int = 0
    completion: int = 0


class GenerateResponse(BaseModel):
    """Successful LLM generation response."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    result_kind: Literal["code"] = "code"
    content: str
    model: str
    tier: Literal["backend"] = "backend"
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    request_id: UUID
