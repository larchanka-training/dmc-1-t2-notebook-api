"""HTTP controllers for ``/notebooks``.

Тонкий слой: контроллеры разворачивают зависимости (текущий
пользователь, сервис), вызывают метод сервиса и возвращают результат.
Бизнес-логика и валидация инвариантов — в :class:`NotebookService`.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.errors import ApiErrorResponse
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.notebooks.dependencies import get_notebook_service
from app.modules.notebooks.schemas.notebook_schemas import (
    NotebookCreate,
    NotebookListResponse,
    NotebookPatch,
    NotebookResponse,
)
from app.modules.notebooks.services.notebook_service import NotebookService

router = APIRouter(prefix="/notebooks", tags=["Notebooks"])


@router.post(
    "",
    response_model=NotebookResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": NotebookResponse,
            "description": "Notebook already existed for this owner",
        },
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
        409: {
            "model": ApiErrorResponse,
            "description": "Notebook id already exists with different content",
        },
    },
    summary="Create notebook",
)
def create_notebook(
    payload: NotebookCreate,
    response: Response,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    """``POST /notebooks`` — create or idempotently return a notebook.

    Возвращает 201 при создании, 200 при идемпотентном повторе,
    409 при идемпотентном повторе с другим содержимым. Подробности —
    в :meth:`NotebookService.create`.

    Args:
        payload: Тело запроса с ``id`` (опционально), ``title``, ``cells``.
        response: FastAPI-объект ответа (нужен, чтобы понизить статус
            с 201 до 200 при идемпотентном попадании).
        current_user: Авторизованный пользователь.
        service: DI-инстанс сервиса.

    Returns:
        Созданный или существующий ноутбук.
    """
    notebook, created = service.create(current_user, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return notebook


@router.get(
    "",
    response_model=NotebookListResponse,
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
    },
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
    """``GET /notebooks`` — paginated list of the user's notebooks.

    Query-параметры ``limit``/``offset`` валидируются FastAPI (``ge``/``le``);
    ``sort``/``order`` валидируются сервисом по whitelist.

    Args:
        limit: Размер страницы (1..200, по умолчанию 50).
        offset: Смещение (≥ 0).
        sort: Поле сортировки (``updatedAt`` / ``createdAt`` / ``title``).
        order: Направление (``asc`` / ``desc``).
        current_user: Авторизованный пользователь.
        service: DI-инстанс сервиса.

    Returns:
        Страница :class:`NotebookListResponse`.
    """
    return service.list(current_user, limit, offset, sort, order)


@router.get(
    "/{notebook_id}",
    response_model=NotebookResponse,
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
    },
    summary="Get notebook",
)
def get_notebook(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    """``GET /notebooks/{id}`` — fetch a single notebook by id.

    Args:
        notebook_id: UUID ноутбука.
        current_user: Авторизованный пользователь.
        service: DI-инстанс сервиса.

    Returns:
        :class:`NotebookResponse`.
    """
    return service.get(current_user, notebook_id)


@router.patch(
    "/{notebook_id}",
    response_model=NotebookResponse,
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
    },
    summary="Patch notebook",
)
def patch_notebook(
    notebook_id: UUID,
    payload: NotebookPatch,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> NotebookResponse:
    """``PATCH /notebooks/{id}`` — apply offline sync document.

    Тело — full sync document (полный набор ``cells`` + ``deletedCells``).
    Сервер делает LWW-merge и возвращает обновлённый ноутбук.

    Args:
        notebook_id: UUID ноутбука.
        payload: Sync-документ от клиента.
        current_user: Авторизованный пользователь.
        service: DI-инстанс сервиса.

    Returns:
        Обновлённый ноутбук после merge.
    """
    return service.patch(current_user, notebook_id, payload)


@router.delete(
    "/{notebook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
    },
    summary="Delete notebook",
)
def delete_notebook(
    notebook_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotebookService = Depends(get_notebook_service),
) -> None:
    """``DELETE /notebooks/{id}`` — soft-delete a notebook.

    Физически запись остаётся в БД, но получает ``deleted_at``. Все
    последующие чтения по этому id будут отдавать 404.

    Args:
        notebook_id: UUID ноутбука.
        current_user: Авторизованный пользователь.
        service: DI-инстанс сервиса.
    """
    service.delete(current_user, notebook_id)
