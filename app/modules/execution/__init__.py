"""Backend code-execution module (debug/fallback ``POST /api/v1/execute``)."""

from app.modules.execution.controllers.execution_controller import router

__all__ = ["router"]
