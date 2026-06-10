"""Dependency wiring for the execution module.

Two responsibilities:

* :func:`require_execute_enabled` — the feature flag gate. The route is always
  registered (so the OpenAPI snapshot is deterministic regardless of
  environment), but when ``ENABLE_EXECUTE`` is false the gate returns
  ``503 execute_disabled``. This is the documented "disabled" behaviour
  (docs/execution-architecture.md §12).
* :func:`get_execution_service` — the service factory, overridable in tests.
"""

from fastapi import Depends, HTTPException, status

from app.core.config import settings
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.execution.services.execution_service import (
    ExecutionService,
    build_execution_service,
)


def require_execute_enabled() -> None:
    """Reject the request when the execution endpoint is disabled by config."""
    if not settings.enable_execute:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "execute_disabled",
                "message": (
                    "Backend code execution is disabled "
                    "(set ENABLE_EXECUTE=true to enable)."
                ),
            },
        )


def get_execution_service() -> ExecutionService:
    """Return the configured execution service."""
    return build_execution_service()


def require_execution_user(
    _: None = Depends(require_execute_enabled),
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Gate on the feature flag first, then require an authenticated user.

    Ordering matters: a disabled endpoint returns ``503`` without forcing the
    caller to authenticate, while an enabled endpoint sits behind Bearer auth
    (docs/authentication-architecture.md §7.3).
    """
    return current_user
