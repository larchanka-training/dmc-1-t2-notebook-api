"""HTTP controllers for ``/notebooks/{notebook_id}/ai-context``.

A notebook sub-resource holding the rolled-up AI generation context (Epic 07 /
#116). Thin layer: resolve auth + service, delegate to
:class:`AiContextService`. All routes are owner-scoped via the notebooks service
(404 when the notebook is missing/deleted, 403 when not owned).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.errors import ApiErrorResponse
from app.modules.ai_context.dependencies import (
    enforce_ai_context_body_size,
    get_ai_context_service,
)
from app.modules.ai_context.schemas.ai_context_schemas import (
    AiContextResponse,
    AiContextStoreRequest,
)
from app.modules.ai_context.services.ai_context_service import AiContextService
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser

router = APIRouter(prefix="/notebooks/{notebook_id}/ai-context", tags=["AI Context"])

_OWNER_SCOPE_RESPONSES = {
    401: {"model": ApiErrorResponse, "description": "Missing or invalid access token"},
    403: {"model": ApiErrorResponse, "description": "Notebook owned by another user"},
    404: {"model": ApiErrorResponse, "description": "Notebook not found"},
}


@router.get(
    "",
    response_model=AiContextResponse,
    responses=_OWNER_SCOPE_RESPONSES,
    summary="Get the stored AI generation context for a notebook",
)
def get_ai_context(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: AiContextService = Depends(get_ai_context_service),
) -> AiContextResponse:
    """Return the persisted, budget-fit context (empty default if never built)."""
    return service.get(current_user, notebook_id)


@router.put(
    "",
    response_model=AiContextResponse,
    responses={
        **_OWNER_SCOPE_RESPONSES,
        422: {
            "model": ApiErrorResponse,
            "description": "Body too large or stored context exceeds the byte limit",
        },
    },
    summary="Store (and roll up) the AI generation context for a notebook",
)
def put_ai_context(
    notebook_id: UUID,
    payload: AiContextStoreRequest,
    current_user: CurrentUser = Depends(get_current_user),
    _: None = Depends(enforce_ai_context_body_size),
    service: AiContextService = Depends(get_ai_context_service),
) -> AiContextResponse:
    """Roll the front-end-built context up to the generation budget and persist."""
    return service.store(current_user, notebook_id, payload)


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_OWNER_SCOPE_RESPONSES,
    summary="Clear the stored AI generation context for a notebook",
)
def delete_ai_context(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: AiContextService = Depends(get_ai_context_service),
) -> None:
    """Drop the stored context (front-end rebuild-on-delete clears it here)."""
    service.clear(current_user, notebook_id)
