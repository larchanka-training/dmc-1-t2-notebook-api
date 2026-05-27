"""Notebooks services — business logic and merge algorithm re-exports."""

from app.modules.notebooks.services.notebook_merge import merge_cells
from app.modules.notebooks.services.notebook_service import NotebookService

__all__ = ["NotebookService", "merge_cells"]
