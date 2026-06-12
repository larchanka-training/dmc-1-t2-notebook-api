"""Business logic for per-notebook AI context persistence.

Verifies notebook ownership (via the notebooks service — 404/403 semantics),
rolls the front-end-built context up to the generation budget through the
pluggable summary service, and persists the budget-fit result. The roll-up is
**budget-aware**: it fires whenever the assembled context would exceed the
8 KiB / 10-item generation budget. The raw PUT body is itself bounded by
``llm_max_prompt_bytes`` (rejected with 422 at the schema boundary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.core.config import settings
from app.core.time import datetime_to_unix_ms
from app.modules.ai_context.models.ai_context import NotebookAiContext
from app.modules.ai_context.repositories.ai_context_repository import (
    AiContextRepository,
)
from app.modules.ai_context.schemas.ai_context_schemas import (
    AiContextResponse,
    AiContextStoreRequest,
)
from app.modules.ai_context.services.summary import SummaryStrategy
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.schemas.llm_schemas import LlmContextCell
from app.modules.notebooks.services.notebook_service import NotebookService


class AiContextService:
    """Store/load the rolled-up AI context for a notebook."""

    def __init__(
        self,
        repository: AiContextRepository,
        notebook_service: NotebookService,
        summary_strategy: SummaryStrategy,
    ) -> None:
        self._repo = repository
        self._notebooks = notebook_service
        self._summary = summary_strategy

    def get(self, current_user: CurrentUser, notebook_id: UUID) -> AiContextResponse:
        """Return the stored context (empty default if never built).

        Raises:
            HTTPException: 404 if the notebook is missing/deleted, 403 if not
                owned by the caller (delegated to the notebooks service).
        """
        self._notebooks.get(current_user, notebook_id)  # ownership + existence
        row = self._repo.get(notebook_id)
        # Defence-in-depth: never surface a row whose denormalised owner_id does
        # not match the caller (the notebook check above already guarantees it;
        # a mismatch would be stale/corrupt state, so fall back to empty).
        if row is None or row.owner_id != current_user.id:
            return AiContextResponse(notebook_id=notebook_id)
        return self._to_response(row)

    def store(
        self,
        current_user: CurrentUser,
        notebook_id: UUID,
        payload: AiContextStoreRequest,
    ) -> AiContextResponse:
        """Roll the built context up to budget and persist it."""
        self._notebooks.get(current_user, notebook_id)  # ownership + existence

        # The raw PUT body is byte-bounded at the schema boundary
        # (llm_max_prompt_bytes); the summary service then compacts it to the
        # 8 KiB / 10-item generation budget.
        result = self._summary.summarize(
            list(payload.context), byte_cap=settings.llm_max_prompt_bytes
        )
        # history_count is the RAW number of histories that fed this build
        # (front-end-trusted), not the rolled-up item count. It is stored as-is
        # and intentionally NOT cross-checked against len(result.context); it is
        # diagnostics only, not an authorization/budget input.
        history_count = (
            payload.history_count
            if payload.history_count is not None
            else len(payload.context)
        )
        row = self._repo.upsert(
            notebook_id=notebook_id,
            owner_id=current_user.id,
            context=[cell.model_dump() for cell in result.context],
            summary=result.summary,
            history_count=history_count,
            updated_at=datetime.now(UTC),
        )
        return self._to_response(row)

    def clear(self, current_user: CurrentUser, notebook_id: UUID) -> None:
        """Drop the stored context (used by the FE rebuild-on-delete flow)."""
        self._notebooks.get(current_user, notebook_id)  # ownership + existence
        self._repo.delete(notebook_id)

    def _to_response(self, row: NotebookAiContext) -> AiContextResponse:
        return AiContextResponse(
            notebook_id=row.notebook_id,
            context=[LlmContextCell(**cell) for cell in (row.context or [])],
            summary=row.summary or "",
            history_count=row.history_count,
            updated_at=datetime_to_unix_ms(row.updated_at),
        )
