"""Domain entities for the notebooks module.

These objects are storage-neutral. Services use them for business logic;
repositories translate them to and from concrete persistence models.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class NotebookEntity:
    """Storage-neutral Notebook aggregate."""

    id: UUID
    owner_id: UUID
    title: str
    format_version: int
    cells: list[dict]
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
