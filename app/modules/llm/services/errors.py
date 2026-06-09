"""Domain exceptions for the LLM generation pipeline."""


class LlmServiceError(Exception):
    """Base class for handled LLM service failures."""

    code = "llm_error"
    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.headers = headers or {}


class LlmProviderNotConfiguredError(LlmServiceError):
    """Raised when the Bedrock provider cannot be used with current settings."""

    code = "llm_provider_not_configured"
    status_code = 503


class LlmProviderError(LlmServiceError):
    """Raised when Bedrock fails or returns an unsupported payload."""

    code = "llm_provider_error"
    status_code = 502


class PromptRejectedError(LlmServiceError):
    """Raised when the guard model rejects the user prompt."""

    code = "prompt_rejected"
    status_code = 422


class CodeValidationError(LlmServiceError):
    """Raised when generated code cannot be validated after retries."""

    code = "code_validation_failed"
    status_code = 422


class LlmTimeoutError(LlmServiceError):
    """Raised when the whole generation pipeline exceeds its deadline.

    The cap is documented in ``docs/requirements.md`` as ``LLM-NF-01``
    (30 seconds end-to-end). Individual Bedrock calls have their own
    per-request timeout, but the orchestration could otherwise stack
    guard + generate + repair attempts and breach the contract.
    """

    code = "llm_timeout"
    status_code = 504
