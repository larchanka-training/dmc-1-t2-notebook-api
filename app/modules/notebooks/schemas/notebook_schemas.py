from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

CURRENT_FORMAT_VERSION = 1
ALLOWED_SORTS = {"updatedAt", "createdAt", "title"}
ALLOWED_ORDERS = {"asc", "desc"}
MAX_CELL_CONTENT_BYTES = 256 * 1024
MAX_CELLS_PER_NOTEBOOK = 500


class CellSchema(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    kind: Literal["code", "markdown"]
    content: str = Field(default="", max_length=MAX_CELL_CONTENT_BYTES)
    updated_at: int = Field(..., ge=0)


class CellTombstone(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: UUID
    deleted_at: int = Field(..., ge=0)


class NotebookBase(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    title: str = Field(..., min_length=1, max_length=255)
    format_version: int = Field(default=CURRENT_FORMAT_VERSION, ge=1)
    cells: list[CellSchema] = Field(
        default_factory=list, max_length=MAX_CELLS_PER_NOTEBOOK
    )

    @model_validator(mode="after")
    def validate_unique_cell_ids(self) -> "NotebookBase":
        ids = [cell.id for cell in self.cells]
        if len(ids) != len(set(ids)):
            raise ValueError("cells must have unique ids")
        return self


class NotebookCreate(NotebookBase):
    id: UUID | None = None


class NotebookPatch(NotebookBase):
    deleted_cells: list[CellTombstone] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_deleted_cell_ids(self) -> "NotebookPatch":
        ids = [cell.id for cell in self.deleted_cells]
        if len(ids) != len(set(ids)):
            raise ValueError("deleted_cells must have unique ids")
        return self


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
