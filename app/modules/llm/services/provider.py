"""Provider-neutral boundary for cloud LLM adapters.

Roadmap Step 8d-1. Both the normalized response shape and the ``LlmProvider``
protocol used to live inside ``bedrock_client``/``generation_service``, which made
Bedrock look like *the* provider rather than *a* provider. They live here now so a
second adapter can be added without importing anything Bedrock-specific.

The old import sites keep working: ``bedrock_client`` and ``generation_service``
re-export these names, so existing callers and tests are unaffected.

An adapter's job is narrow: take a model id, a system prompt and a user prompt,
call the vendor, and return :class:`LlmProviderResponse` — or raise one of the
errors in ``services.errors`` with the same semantics every other adapter uses.
Prompt building, guard logic, validation and repair stay in the generation
service and must not be duplicated per provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class LlmProviderResponse:
    """Normalized model response returned by every provider adapter."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LlmProvider(Protocol):
    """Provider boundary used by the generation service, tests and adapters."""

    def converse(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        """Return a normalized provider response."""
