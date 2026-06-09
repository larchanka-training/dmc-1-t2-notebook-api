"""HTTP controller for cloud LLM code generation."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import settings
from app.core.errors import ApiErrorResponse
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.dependencies import (
    enforce_llm_body_size,
    enforce_llm_rate_limit,
    get_llm_generation_service,
)
from app.modules.llm.schemas.llm_schemas import GenerateRequest, GenerateResponse
from app.modules.llm.services.errors import LlmServiceError, LlmTimeoutError
from app.modules.llm.services.generation_service import LlmGenerationService

router = APIRouter(prefix="/llm", tags=["LLM"])

# Pipeline runs on a dedicated executor so the outer ``Future.result(timeout=…)``
# can enforce ``LLM-NF-01`` (30-second end-to-end cap) regardless of how many
# Bedrock and esbuild calls the orchestrator chained internally. The worker
# count is small on purpose: each ``/llm/generate`` already burns one of
# Starlette's threadpool workers, and Bedrock calls are billed.
_PIPELINE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-pipeline")


@router.post(
    "/generate",
    response_model=GenerateResponse,
    responses={
        401: {
            "model": ApiErrorResponse,
            "description": "Missing or invalid access token",
        },
        422: {
            "model": ApiErrorResponse,
            "description": "Prompt rejected or generated code failed validation",
        },
        429: {
            "model": ApiErrorResponse,
            "description": "Per-user LLM rate limit exceeded",
        },
        500: {
            "model": ApiErrorResponse,
            "description": "Internal LLM provider configuration error",
        },
        502: {
            "model": ApiErrorResponse,
            "description": "Bedrock provider failed",
        },
        503: {
            "model": ApiErrorResponse,
            "description": "Bedrock provider is not configured",
        },
        504: {
            "model": ApiErrorResponse,
            "description": "Generation pipeline exceeded the configured deadline",
        },
    },
    status_code=status.HTTP_200_OK,
    summary="Generate code via the Cloud LLM agent",
)
def generate_code(
    payload: GenerateRequest,
    # Order matters. FastAPI resolves dependencies in declaration
    # order. We authenticate first (``enforce_llm_rate_limit`` pulls
    # ``get_current_user``) so anonymous callers never trigger the
    # ``await request.body()`` buffering inside ``enforce_llm_body_size``.
    current_user: CurrentUser = Depends(enforce_llm_rate_limit),
    _: None = Depends(enforce_llm_body_size),
    service: LlmGenerationService = Depends(get_llm_generation_service),
) -> GenerateResponse:
    """Generate validated JavaScript/TypeScript code for an authenticated user.

    The whole guard → generate → validate → repair pipeline runs inside
    a worker thread and is capped by ``settings.llm_request_timeout_seconds``
    (``LLM-NF-01``). Exceeding the deadline raises
    :class:`LlmTimeoutError` → ``504 llm_timeout``. The in-flight worker
    keeps running until Bedrock returns, but the HTTP response is no
    longer blocked by it.
    """
    future = _PIPELINE_EXECUTOR.submit(service.generate, payload, current_user)
    try:
        result = future.result(timeout=settings.llm_request_timeout_seconds)
    except FuturesTimeoutError as exc:
        raise HTTPException(
            status_code=LlmTimeoutError.status_code,
            detail={
                "code": LlmTimeoutError.code,
                "message": (
                    "LLM generation exceeded the "
                    f"{settings.llm_request_timeout_seconds}s pipeline deadline"
                ),
            },
        ) from exc
    except LlmServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
            headers=exc.headers,
        ) from exc

    return result
