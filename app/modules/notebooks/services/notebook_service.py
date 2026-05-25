from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.core.time import datetime_to_unix_ms, unix_ms_to_datetime
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.notebooks.models.notebook import Notebook
from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.schemas.notebook_schemas import (
    ALLOWED_ORDERS,
    ALLOWED_SORTS,
    CURRENT_FORMAT_VERSION,
    CellSchema,
    CellTombstone,
    NotebookCreate,
    NotebookListItem,
    NotebookListResponse,
    NotebookPatch,
    NotebookResponse,
)
from app.modules.notebooks.services.notebook_merge import merge_cells

MAX_FUTURE_SKEW_MS = 5_000


def notebook_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOTEBOOK_NOT_FOUND", "message": "Notebook not found"},
    )


def forbidden() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "FORBIDDEN", "message": "Forbidden"},
    )


def invalid_query(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "INVALID_QUERY", "message": message},
    )


def unsupported_format_version() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "code": "UNSUPPORTED_FORMAT_VERSION",
            "message": f"Only formatVersion <= {CURRENT_FORMAT_VERSION} is supported",
        },
    )


class NotebookService:
    def __init__(self, repository: NotebookRepository) -> None:
        self.repository = repository

    def create(
        self,
        current_user: CurrentUser,
        payload: NotebookCreate,
    ) -> tuple[NotebookResponse, bool]:
        notebook_id = payload.id or uuid4()
        existing = self.repository.get_by_id(notebook_id)
        if existing is not None:
            if existing.owner_id != current_user.id:
                raise forbidden()
            if existing.deleted_at is not None:
                raise notebook_not_found()
            return self.to_response(existing), False

        self._validate_format_version(payload.format_version)
        now = datetime.now(UTC)
        cells = self._cells_to_storage(payload.cells)
        updated_at = self._compute_updated_at(cells, now)
        notebook = Notebook(
            id=notebook_id,
            owner_id=current_user.id,
            title=payload.title,
            format_version=payload.format_version,
            cells=cells,
            created_at=now,
            updated_at=updated_at,
            deleted_at=None,
        )
        return self.to_response(self.repository.create(notebook)), True

    def list(
        self,
        current_user: CurrentUser,
        limit: int,
        offset: int,
        sort: str,
        order: str,
    ) -> NotebookListResponse:
        if sort not in ALLOWED_SORTS:
            raise invalid_query("Unsupported sort field")
        if order not in ALLOWED_ORDERS:
            raise invalid_query("Unsupported order")

        items, total = self.repository.list_by_owner(
            current_user.id,
            limit,
            offset,
            sort,
            order,
        )
        return NotebookListResponse(
            items=[self.to_list_item(item) for item in items],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get(self, current_user: CurrentUser, notebook_id: UUID) -> NotebookResponse:
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        return self.to_response(notebook)

    def patch(
        self,
        current_user: CurrentUser,
        notebook_id: UUID,
        payload: NotebookPatch,
    ) -> NotebookResponse:
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        self._validate_format_version(payload.format_version)

        client_cells = self._cells_to_storage(payload.cells)
        deleted_cells = self._tombstones_to_storage(payload.deleted_cells)
        merged_cells = merge_cells(notebook.cells or [], client_cells, deleted_cells)

        now = datetime.now(UTC)
        notebook.title = payload.title
        notebook.format_version = payload.format_version
        notebook.cells = merged_cells
        notebook.updated_at = self._compute_updated_at(merged_cells, now)
        return self.to_response(self.repository.save(notebook))

    def delete(self, current_user: CurrentUser, notebook_id: UUID) -> None:
        notebook = self._get_active_notebook(notebook_id)
        self._ensure_owner(notebook, current_user)
        self.repository.soft_delete(notebook, datetime.now(UTC))

    def _get_active_notebook(self, notebook_id: UUID) -> Notebook:
        notebook = self.repository.get_by_id(notebook_id)
        if notebook is None or notebook.deleted_at is not None:
            raise notebook_not_found()
        return notebook

    def _ensure_owner(self, notebook: Notebook, current_user: CurrentUser) -> None:
        if notebook.owner_id != current_user.id:
            raise forbidden()

    def _validate_format_version(self, format_version: int) -> None:
        if format_version > CURRENT_FORMAT_VERSION:
            raise unsupported_format_version()

    def _compute_updated_at(
        self,
        cells: list[dict],
        fallback: datetime,
    ) -> datetime:
        if not cells:
            return fallback
        latest_cell_ms = max(int(cell["updatedAt"]) for cell in cells)
        now_ms = int(time.time() * 1000)
        latest = min(latest_cell_ms, now_ms + MAX_FUTURE_SKEW_MS)
        latest = max(latest, datetime_to_unix_ms(fallback))
        return unix_ms_to_datetime(latest)

    def _cells_to_storage(self, cells: list[CellSchema]) -> list[dict]:
        # Keep JSONB cells API-shaped; FE sync depends on camelCase keys.
        return [cell.model_dump(by_alias=True, mode="json") for cell in cells]

    def _tombstones_to_storage(self, tombstones: list[CellTombstone]) -> list[dict]:
        return [
            tombstone.model_dump(by_alias=True, mode="json") for tombstone in tombstones
        ]

    def to_response(self, notebook: Notebook) -> NotebookResponse:
        return NotebookResponse(
            id=notebook.id,
            owner_id=notebook.owner_id,
            title=notebook.title,
            format_version=notebook.format_version,
            cells=notebook.cells or [],
            created_at=datetime_to_unix_ms(notebook.created_at),
            updated_at=datetime_to_unix_ms(notebook.updated_at),
        )

    def to_list_item(self, notebook: Notebook) -> NotebookListItem:
        return NotebookListItem(
            id=notebook.id,
            title=notebook.title,
            format_version=notebook.format_version,
            created_at=datetime_to_unix_ms(notebook.created_at),
            updated_at=datetime_to_unix_ms(notebook.updated_at),
            cells_count=len(notebook.cells or []),
        )
