"""FastAPI dependencies for the LLM module."""

from fastapi import Depends, HTTPException, Request, status

from app.core.config import settings
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
    """Reject oversized generation request bodies before invoking LLMs.

    Two-stage size enforcement:

    1. Short-circuit via the ``Content-Length`` request header **before**
       buffering the body — this prevents a hostile client from streaming
       a multi-megabyte payload into memory just to be rejected. Servers
       and proxies typically validate ``Content-Length`` against the
       transferred bytes, but we treat the header as a defensive hint.
    2. Buffer and re-check the actual byte length. The header is
       advisory; a malformed/missing header is allowed to fall through to
       the buffered check.
    """
    total_cap = settings.llm_max_total_bytes
    cap_kib = total_cap // 1024
    error_message = f"LLM generation request body exceeds the {cap_kib} KiB limit"

    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared = int(content_length_header)
        except ValueError:
            declared = -1  # malformed header — fall through to buffered check
        if declared > total_cap:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "request_too_large", "message": error_message},
            )

    body = await request.body()
    if len(body) > total_cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "request_too_large", "message": error_message},
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
