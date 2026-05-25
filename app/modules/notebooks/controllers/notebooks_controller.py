from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.schemas.notebook_schemas import (
    NotebookCreate,
    NotebookListResponse,
    NotebookPatch,
    NotebookResponse,
)
from app.modules.notebooks.services.notebook_service import NotebookService

router = APIRouter(prefix="/notebooks", tags=["Notebooks"])


def get_notebook_service(db: Session = Depends(get_db)) -> NotebookService:
    return NotebookService(NotebookRepository(db))


@router.post(
    "",
    response_model=NotebookResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": NotebookResponse,
            "description": "Notebook already existed for this owner",
        }
    },
    summary="Create notebook",
)
def create_notebook(
    payload: NotebookCreate,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    notebook, created = service.create(current_user, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return notebook


@router.get(
    "",
    response_model=NotebookListResponse,
    summary="List notebooks",
)
def list_notebooks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: str = Query(default="updatedAt"),
    order: str = Query(default="desc"),
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookListResponse:
    return service.list(current_user, limit, offset, sort, order)


@router.get(
    "/{notebook_id}",
    response_model=NotebookResponse,
    summary="Get notebook",
)
def get_notebook(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    return service.get(current_user, notebook_id)


@router.patch(
    "/{notebook_id}",
    response_model=NotebookResponse,
    summary="Patch notebook",
)
def patch_notebook(
    notebook_id: UUID,
    payload: NotebookPatch,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    return service.patch(current_user, notebook_id, payload)


@router.delete(
    "/{notebook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete notebook",
)
def delete_notebook(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> None:
    service.delete(current_user, notebook_id)
