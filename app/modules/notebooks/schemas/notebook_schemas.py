from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CURRENT_FORMAT_VERSION = 1
ALLOWED_SORTS = {"updatedAt", "createdAt", "title"}
ALLOWED_ORDERS = {"asc", "desc"}


class CellSchema(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    kind: Literal["code", "markdown"]
    content: str = ""
    updated_at: int = Field(..., ge=0)


class CellTombstone(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    deleted_at: int = Field(..., ge=0)


class NotebookBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=255)
    format_version: int = Field(default=CURRENT_FORMAT_VERSION, ge=1)
    cells: list[CellSchema] = Field(default_factory=list)


class NotebookCreate(NotebookBase):
    id: UUID | None = None


class NotebookPatch(NotebookBase):
    deleted_cells: list[CellTombstone] = Field(default_factory=list)


class NotebookResponse(NotebookBase):
    id: UUID
    owner_id: UUID
    created_at: int
    updated_at: int


class NotebookListItem(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    title: str
    format_version: int
    created_at: int
    updated_at: int
    cells_count: int


class NotebookListResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    items: list[NotebookListItem]
    total: int
    limit: int
    offset: int
