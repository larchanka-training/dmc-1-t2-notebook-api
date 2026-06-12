"""AI context module.

Per-notebook persistence of the AI code-generation context (Epic 07 / #116).
The front-end builds context from the notebook's cells; this module stores it
server-side and rolls it up to the generation budget via a pluggable,
budget-aware summary service. Classic slice:

* ``models`` — SQLAlchemy ORM for ``notebooks.notebook_ai_context``;
* ``schemas`` — Pydantic request/response DTOs (reusing ``LlmContextCell``);
* ``repositories`` — DAL over ``Session``;
* ``services`` — store/load logic + the summary strategies;
* ``controllers`` — HTTP routes under ``/api/v1/notebooks/{id}/ai-context``.
"""

from app.modules.ai_context.controllers import router

__all__ = ["router"]
