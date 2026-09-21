"""FastAPI dependencies for the LLM module."""

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.db import get_session_factory
from app.core.request_limits import enforce_body_size
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.services.generation_service import (
    LlmGenerationService,
    build_generation_service,
)
from app.modules.llm.services.rate_limiter import InMemoryRateLimiter
from app.modules.llm.services.reconciliation_service import LlmReconciliationService
from app.modules.llm.services.usage_service import LlmUsageService

_rate_limiter = InMemoryRateLimiter(
    limit=settings.llm_rate_limit_per_minute,
    window_seconds=60,
)


def get_llm_generation_service() -> LlmGenerationService:
    """Return the configured LLM generation service."""
    return build_generation_service()


def get_llm_usage_service() -> LlmUsageService:
    """Return the configured LLM usage service."""
    return LlmUsageService(session_factory=get_session_factory(), settings=settings)


def get_llm_reconciliation_service() -> LlmReconciliationService:
    """Return the configured LLM reconciliation service."""
    return LlmReconciliationService(
        session_factory=get_session_factory(), settings=settings
    )


def get_rate_limiter() -> InMemoryRateLimiter:
    """Return the process-local LLM rate limiter."""
    return _rate_limiter


async def enforce_llm_body_size(request: Request) -> None:
    """Reject oversized generation request bodies before invoking LLMs."""
    total_cap = settings.llm_max_total_bytes
    await enforce_body_size(
        request,
        max_bytes=total_cap,
        error_message=(
            f"LLM generation request body exceeds the {total_cap // 1024} KiB limit"
        ),
    )


def enforce_llm_access(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Restrict the cloud LLM endpoint to the configured developer allowlist.

    An EMPTY ``LLM_ALLOWED_EMAILS`` means no restriction, so existing deployments
    are unaffected. A non-empty list restricts the endpoint to those account
    emails.

    Why this exists (roadmap Step 8d-1): while the provider runs on a shared free
    tier, the daily request quota belongs to the DEPLOYMENT, not to a user — the
    per-user rate limit caps how fast one account spends it, but not how many
    accounts spend it, so any signed-in user could exhaust the day's quota for
    everyone. This is the control that makes free-tier operation viable.

    It is a real, server-side authorization check — unlike the UI's `llmEnabled`
    switch, which is a device-local preference. Matching is case-insensitive; a
    user with no email on the token is denied whenever an allowlist is configured,
    since there is nothing to match against.
    """
    allowed = settings.llm_allowed_email_set
    if not allowed:
        return current_user

    email = (current_user.email or "").strip().lower()
    if email not in allowed:
        # Do not echo the allowlist or the caller's email back: the message is the
        # same for "not on the list" and "no email on the token".
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "llm_access_denied",
                "message": "Cloud LLM generation is limited to allowlisted accounts",
            },
        )
    return current_user


def enforce_llm_admin_access(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Restrict LLM admin operations to configured administrators.

    Fail-closed policy:
    1. If user has 'admin' in current_user.roles, access is granted.
    2. Otherwise, check settings.llm_admin_email_set, falling back to
       settings.llm_allowed_email_set.
    3. If the resolved admin allowlist is empty or whitespace, access is DENIED (HTTP 403).
       Unlike generation access (where empty means unrestricted public generation),
       admin routes (global telemetry, background reconciliation) are never open
       to unallowlisted users.
    4. If the caller's email is not in the allowlist, access is DENIED (HTTP 403).
    """
    if "admin" in (current_user.roles or []):
        return current_user

    allowed = settings.llm_admin_email_set or settings.llm_allowed_email_set
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "llm_admin_access_denied",
                "message": "LLM admin operations are limited to allowlisted administrator accounts",
            },
        )

    email = (current_user.email or "").strip().lower()
    if not email or email not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "llm_admin_access_denied",
                "message": "LLM admin operations are limited to allowlisted administrator accounts",
            },
        )
    return current_user


def enforce_llm_rate_limit(
    current_user: CurrentUser = Depends(get_current_user),
    limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> CurrentUser:
    """Enforce the per-user LLM request limit and return the current user."""
    retry_after = limiter.check(current_user.id)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            detail={
                "code": "rate_limited",
                "message": "LLM request rate limit exceeded",
            },
        )
    return current_user
