"""FastAPI dependencies for the ai_context module."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.request_limits import enforce_body_size
from app.modules.ai_context.repositories.ai_context_repository import (
    AiContextRepository,
)
from app.modules.ai_context.services.ai_context_service import AiContextService
from app.modules.ai_context.services.summary import build_summary_service
from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.services.notebook_service import NotebookService


def get_ai_context_service(db: Session = Depends(get_db)) -> AiContextService:
    """Assemble a request-scoped :class:`AiContextService`.

    Wires the context repository, the notebooks service (for ownership checks),
    and the env-selected summary strategy. ``build_summary_service`` is
    process-cached, so the (possibly Bedrock-backed) strategy is built once, not
    per request. Overridable in tests via ``app.dependency_overrides``.
    """
    return AiContextService(
        AiContextRepository(db),
        NotebookService(NotebookRepository(db)),
        build_summary_service(),
    )


async def enforce_ai_context_body_size(request: Request) -> None:
    """Reject an oversized PUT body before it is parsed (shared input guard)."""
    cap = settings.llm_max_total_bytes
    await enforce_body_size(
        request,
        max_bytes=cap,
        error_message=f"AI context request body exceeds the {cap // 1024} KiB limit",
    )
