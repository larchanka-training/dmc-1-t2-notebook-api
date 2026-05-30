"""Notebooks repositories.

Re-exports the SQLAlchemy implementation (:class:`NotebookRepository`)
and the storage contract (:class:`NotebookRepositoryProtocol`).
Service typings against the protocol; DI wires the implementation —
see :mod:`app.modules.notebooks.dependencies`.
"""

from app.modules.notebooks.repositories.notebook_repository import NotebookRepository
from app.modules.notebooks.repositories.protocol import NotebookRepositoryProtocol

__all__ = ["NotebookRepository", "NotebookRepositoryProtocol"]
