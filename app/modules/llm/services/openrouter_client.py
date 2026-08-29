"""OpenRouter adapter for code generation (roadmap Step 8d-1).

OpenRouter exposes an OpenAI-compatible ``POST /chat/completions``, so this
adapter is a thin HTTP client that speaks that shape and normalizes the result
into :class:`LlmProviderResponse` — the same type the Bedrock adapter returns.
Everything above it (guard pass, prompt building, validation, repair, rate
limiting, byte caps, metadata-only logging) is provider-neutral and unchanged.

Security boundary (``docs/specs/llm-provider-toggle-security-contract.md`` and
``llm-openrouter-replacement-decision.md``):

- the base URL is a **module constant**, not configuration. A free-form
  ``LLM_BASE_URL`` would let whoever controls deployment config point the server
  at an arbitrary host — the same SSRF shape that was rejected for user-supplied
  endpoints, merely with a different actor. Adding a provider means adding an
  adapter, not editing a URL.
- the API key is read from server-side settings, never from the request, and
  never logged. Only metadata (model, token counts, status) is logged.

Why ``urllib`` and not ``httpx``/``requests``: this needs one JSON POST with a
timeout, and ``AGENTS.md`` §11 forbids adding a runtime dependency without
approval (``httpx`` is currently a TEST-only dependency, so importing it here
would work in CI and fail in the deployed image). The seam that matters for
testing is the injected ``transport``, not the HTTP library.

Operational note: the free tier is capped per day (50 requests without purchased
credits, 1000 after a >= $10 purchase) and the pipeline spends TWO calls per user
generation, so the free tier is a development affordance, not a shipping path.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from app.core.logging import get_logger
from app.modules.llm.services.errors import (
    LlmProviderError,
    LlmProviderNotConfiguredError,
    LlmServiceError,
)
from app.modules.llm.services.provider import LlmProviderResponse

logger = get_logger(__name__)

#: Fixed by code, deliberately NOT configurable — see the module docstring.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_CHAT_COMPLETIONS_PATH = "/chat/completions"


@dataclass(frozen=True)
class HttpResponse:
    """The minimal HTTP result this adapter needs, independent of the client used."""

    status_code: int
    body: str
    headers: dict[str, str]


#: Injected by tests so the suite never opens a socket. Production always uses
#: :func:`_urllib_transport`.
Transport = Callable[[str, bytes, dict[str, str], float], HttpResponse]


class OpenRouterClient:
    """Minimal OpenAI-compatible chat-completions client for OpenRouter."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int,
        base_url: str = OPENROUTER_BASE_URL,
        app_title: str | None = None,
        app_referer: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url.rstrip("/")
        self.app_title = app_title
        self.app_referer = app_referer
        self._transport: Transport = transport or _urllib_transport

    def converse(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        """Call OpenRouter chat completions and normalize the response."""
        if not self.api_key:
            # Configuration is validated at startup; this guards the case where a
            # service instance is built directly (tests, scripts) without a key.
            raise LlmProviderNotConfiguredError(
                "OpenRouter provider requires LLM_OPENROUTER_API_KEY to be set"
            )

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body = json.dumps(payload).encode("utf-8")
        url = f"{self.base_url}{_CHAT_COMPLETIONS_PATH}"

        try:
            response = self._transport(
                url, body, self._headers(), float(self.timeout_seconds)
            )
        except TimeoutError as exc:
            logger.error("openrouter.invoke.timeout", model=model_id)
            raise LlmProviderError(
                "LLM provider is unavailable",
                code="llm_unavailable",
                status_code=503,
            ) from exc
        except OSError as exc:
            # Never interpolate the exception into the returned message: it can
            # carry the request URL, and a future signed URL would leak with it.
            logger.error("openrouter.invoke.failed", error_type=type(exc).__name__)
            raise LlmProviderError(
                "LLM provider is unavailable",
                code="llm_unavailable",
                status_code=503,
            ) from exc

        if response.status_code >= 400:
            raise _map_status_error(response, model_id)

        return _parse_completion_response(response, model_id)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Optional attribution headers; OpenRouter uses them for leaderboards only.
        if self.app_referer:
            headers["HTTP-Referer"] = self.app_referer
        if self.app_title:
            headers["X-OpenRouter-Title"] = self.app_title
        return headers


def _urllib_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> HttpResponse:
    """Perform the POST with the standard library.

    ``HTTPError`` is a response, not a failure, so it is unwrapped into a normal
    :class:`HttpResponse` — status mapping belongs in one place
    (:func:`_map_status_error`), not split across an exception path.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as raw:  # noqa: S310
            return HttpResponse(
                status_code=raw.status,
                body=raw.read().decode("utf-8", errors="replace"),
                headers={k.lower(): v for k, v in raw.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status_code=exc.code,
            body=exc.read().decode("utf-8", errors="replace"),
            headers={k.lower(): v for k, v in exc.headers.items()},
        )
    except socket.timeout as exc:  # pragma: no cover - platform dependent alias
        raise TimeoutError(str(exc)) from exc


def _map_status_error(response: HttpResponse, model_id: str) -> LlmServiceError:
    """Map an OpenRouter HTTP failure onto the same semantics Bedrock uses.

    The codes deliberately match ``bedrock_client._map_bedrock_error`` so the API
    error contract (and therefore the UI, which keys off ``error.code``) does not
    change when the provider does.
    """
    status = response.status_code
    # The provider message is logged, never returned: it can echo the request, and
    # the UI must not receive provider internals (ai-architecture.md §8.4).
    logger.error(
        "openrouter.invoke.failed",
        status_code=status,
        model=model_id,
        provider_message=_error_message(response),
    )

    if status in (401, 403):
        return LlmProviderError(
            "LLM provider access denied",
            code="llm_access_denied",
            status_code=500,
        )
    if status == 400:
        return LlmProviderError(
            "LLM provider validation failed",
            code="llm_internal",
            status_code=500,
        )
    if status == 429:
        retry_after = response.headers.get("retry-after", "60")
        return LlmServiceError(
            "LLM provider is throttling requests",
            code="llm_throttled",
            status_code=429,
            headers={"Retry-After": retry_after},
        )
    if status in (502, 503, 504):
        return LlmProviderError(
            "LLM provider is unavailable",
            code="llm_unavailable",
            status_code=503,
        )
    return LlmProviderError("OpenRouter model invocation failed")


def _error_message(response: HttpResponse) -> str:
    """Best-effort provider error text for logs only (never returned to a user)."""
    try:
        body: Any = json.loads(response.body)
    except (ValueError, TypeError):
        return response.body[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])[:200]
    return str(body)[:200]


def _parse_completion_response(
    response: HttpResponse,
    model_id: str,
) -> LlmProviderResponse:
    """Normalize the OpenAI-compatible chat-completions payload."""
    try:
        payload: Any = json.loads(response.body)
    except ValueError as exc:
        raise LlmProviderError("OpenRouter response was not valid JSON") from exc

    if not isinstance(payload, dict):
        raise LlmProviderError("OpenRouter response was not a JSON object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmProviderError("OpenRouter response did not include any choices")

    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    text = content.strip() if isinstance(content, str) else ""
    if not text:
        raise LlmProviderError("OpenRouter response was empty")

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    # A router can serve a different model than the one requested, so report what
    # the provider says it used and fall back to the request only if absent.
    served_model = payload.get("model")
    return LlmProviderResponse(
        text=text,
        model=served_model if isinstance(served_model, str) and served_model else model_id,
        prompt_tokens=_int_or_zero(usage.get("prompt_tokens")),
        completion_tokens=_int_or_zero(usage.get("completion_tokens")),
        raw=payload,
    )


def _int_or_zero(value: Any) -> int:
    """Coerce a usage counter, tolerating a provider that omits or nulls it."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
