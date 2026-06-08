"""HTTP controller for cloud LLM code generation."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.errors import ApiErrorResponse
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.dependencies import (
    enforce_llm_body_size,
    enforce_llm_rate_limit,
    get_llm_generation_service,
)
from app.modules.llm.schemas.llm_schemas import GenerateRequest, GenerateResponse
from app.modules.llm.services.errors import LlmServiceError
from app.modules.llm.services.generation_service import LlmGenerationService

router = APIRouter(prefix="/llm", tags=["LLM"])


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
    },
    status_code=status.HTTP_200_OK,
    summary="Generate code via the Cloud LLM agent",
)
def generate_code(
    payload: GenerateRequest,
    _: None = Depends(enforce_llm_body_size),
    current_user: CurrentUser = Depends(enforce_llm_rate_limit),
    service: LlmGenerationService = Depends(get_llm_generation_service),
) -> GenerateResponse:
    """Generate validated JavaScript/TypeScript code for an authenticated user."""
    try:
        return service.generate(payload, current_user)
    except LlmServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
            headers=exc.headers,
        ) from exc
