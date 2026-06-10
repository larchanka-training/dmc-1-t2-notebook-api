"""HTTP controller for ``POST /api/v1/execute``.

Backend code execution is a **debug/fallback** endpoint, disabled by default
(``ENABLE_EXECUTE=false``) and gated behind Bearer auth when enabled. The
runner behind it is **not** a production sandbox — see
``docs/execution-architecture.md`` §12.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.errors import ApiErrorResponse
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.execution.dependencies import (
    get_execution_service,
    require_execution_user,
)
from app.modules.execution.schemas.execution_schemas import (
    ExecuteRequest,
    ExecuteResponse,
)
from app.modules.execution.services.errors import ExecutionError
from app.modules.execution.services.execution_service import ExecutionService

router = APIRouter(prefix="/execute", tags=["Execution"])


@router.post(
    "",
    response_model=ExecuteResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_200_OK,
    summary="Execute notebook cell code on the backend (debug/fallback)",
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
        422: {
            "model": ApiErrorResponse,
            "description": "Invalid request (empty code, non-positive timeout, oversized source)",
        },
        503: {
            "model": ApiErrorResponse,
            "description": (
                "Endpoint disabled (`execute_disabled`) or runtime unavailable"
            ),
        },
    },
)
def execute_code(
    payload: ExecuteRequest,
    current_user: CurrentUser = Depends(require_execution_user),
    service: ExecutionService = Depends(get_execution_service),
) -> ExecuteResponse:
    """Run JavaScript and return a ``cell.outputs``-compatible result.

    Execution *errors of user code* (syntax/runtime/timeout) are not HTTP
    errors — they arrive as ``200`` with ``status`` ∈ ``error | timeout`` and
    structured ``outputs`` (docs/execution-architecture.md §9). HTTP error
    codes are reserved for problems with the request or the runtime itself.

    Args:
        payload: The code, language, and optional ``timeoutMs``.
        current_user: The authenticated caller (feature-flag + auth gate).
        service: The execution service (subprocess runner).

    Returns:
        The unified :class:`ExecuteResponse`.
    """
    try:
        return service.execute(payload, current_user)
    except ExecutionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
