"""AWS Bedrock Runtime adapter for code generation."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from threading import Lock
from typing import Any

from app.modules.llm.services.errors import (
    LlmProviderError,
    LlmProviderNotConfiguredError,
    LlmServiceError,
)

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
except ModuleNotFoundError:  # pragma: no cover - exercised only without dependency.
    boto3 = None
    Config = None
    BotoCoreError = None
    ClientError = None
    EndpointConnectionError = None

_CLIENT_CACHE: dict[tuple[str, int], Any] = {}
_CLIENT_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class LlmProviderResponse:
    """Normalized model response returned by provider adapters."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class BedrockClient:
    """Thin wrapper around Bedrock Runtime Converse API."""

    def __init__(self, region_name: str, timeout_seconds: int) -> None:
        self.region_name = region_name
        self.timeout_seconds = timeout_seconds

    def converse(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        """Call Bedrock Converse and normalize text plus token metadata."""
        client = _get_client(self.region_name, self.timeout_seconds)

        try:
            payload = client.converse(
                modelId=model_id,
                system=[{"text": system_prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": user_prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
        except Exception as exc:
            raise _map_bedrock_error(exc) from exc

        return _parse_converse_response(payload, model_id)


def _get_client(region_name: str, timeout_seconds: int) -> Any:
    """Return a cached Bedrock Runtime client for this process."""
    if boto3 is None or Config is None:
        raise LlmProviderNotConfiguredError(
            "Bedrock provider requires boto3 to be installed"
        )

    key = (region_name, timeout_seconds)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is None:
            client = boto3.client(
                "bedrock-runtime",
                region_name=region_name,
                config=Config(
                    connect_timeout=5,
                    read_timeout=timeout_seconds,
                    retries={"max_attempts": 1},
                ),
            )
            _CLIENT_CACHE[key] = client
        return client


def clear_bedrock_client_cache() -> None:
    """Clear cached clients in tests."""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()


def _map_bedrock_error(exc: Exception) -> LlmServiceError:
    """Map Bedrock/botocore failures to stable API error semantics."""
    if ClientError is not None and isinstance(exc, ClientError):
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        if error_code == "AccessDeniedException":
            return LlmProviderError(
                "LLM provider access denied",
                code="llm_access_denied",
                status_code=500,
            )
        if error_code == "ValidationException":
            return LlmProviderError(
                "LLM provider validation failed",
                code="llm_internal",
                status_code=500,
            )
        if error_code == "ThrottlingException":
            return LlmServiceError(
                "LLM provider is throttling requests",
                code="llm_throttled",
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return LlmProviderError("Bedrock model invocation failed")

    if EndpointConnectionError is not None and isinstance(exc, EndpointConnectionError):
        return LlmProviderError(
            "LLM provider is unavailable",
            code="llm_unavailable",
            status_code=503,
        )

    if BotoCoreError is not None and isinstance(exc, BotoCoreError):
        return LlmProviderError("Bedrock model invocation failed")

    return LlmProviderError("Bedrock model invocation failed")


def _parse_converse_response(
    payload: dict[str, Any],
    model_id: str,
) -> LlmProviderResponse:
    """Normalize the Bedrock Converse response shape."""
    try:
        content = payload["output"]["message"]["content"]
    except KeyError as exc:
        raise LlmProviderError("Bedrock response did not include message content") from exc

    text_parts = [
        str(part["text"])
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    text = "\n".join(text_parts).strip()
    if not text:
        raise LlmProviderError("Bedrock response was empty")

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return LlmProviderResponse(
        text=text,
        model=model_id,
        prompt_tokens=int(usage.get("inputTokens") or 0),
        completion_tokens=int(usage.get("outputTokens") or 0),
        raw=payload,
    )


def parse_guard_json(response_text: str) -> bool:
    """Return whether the guard response marks the prompt as safe."""
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LlmProviderError("Guard model returned invalid JSON") from exc

    safe = payload.get("safe")
    if not isinstance(safe, bool):
        raise LlmProviderError("Guard model JSON must contain boolean safe")
    return safe
