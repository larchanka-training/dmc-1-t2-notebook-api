"""FastAPI dependencies for the LLM module."""

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.request_limits import enforce_body_size
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.services.generation_service import (
    LlmGenerationService,
    build_generation_service,
)
from app.modules.llm.services.rate_limiter import InMemoryRateLimiter

_rate_limiter = InMemoryRateLimiter(
    limit=settings.llm_rate_limit_per_minute,
    window_seconds=60,
)


def get_llm_generation_service() -> LlmGenerationService:
    """Return the configured LLM generation service."""
    return build_generation_service()


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
