"""Unit tests for the OpenRouter adapter (roadmap Step 8d-1).

Every case drives the adapter through an injected fake transport, so the suite
never opens a socket and CI never calls OpenRouter. That is a hard requirement of
the 8d plan, not a convenience: a test that reaches the provider would burn the
shared free-tier daily quota.
"""

import json

import pytest

from app.modules.llm.services.errors import (
    LlmProviderError,
    LlmProviderNotConfiguredError,
    LlmServiceError,
)
from app.modules.llm.services import openrouter_client
from app.modules.llm.services.openrouter_client import (
    HttpResponse,
    OpenRouterClient,
)

COMPLETION_BODY = {
    "id": "gen-1",
    "model": "vendor/some-free-model",
    "choices": [{"message": {"role": "assistant", "content": "const answer = 42"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


def make_client(response: HttpResponse, recorder: list[dict] | None = None):
    """Build a client whose transport returns `response` and records the call."""

    def transport(url, body, headers, timeout):
        if recorder is not None:
            recorder.append(
                {
                    "url": url,
                    "payload": json.loads(body.decode("utf-8")),
                    "headers": headers,
                    "timeout": timeout,
                }
            )
        return response

    return OpenRouterClient(api_key="test-key", timeout_seconds=30, transport=transport)


def ok(body: dict, status: int = 200, headers: dict | None = None) -> HttpResponse:
    return HttpResponse(status_code=status, body=json.dumps(body), headers=headers or {})


def converse(client):
    return client.converse(
        model_id="openrouter/free",
        system_prompt="system",
        user_prompt="user",
        max_tokens=256,
        temperature=0.2,
    )


def test_converse_normalizes_a_successful_completion() -> None:
    result = converse(make_client(ok(COMPLETION_BODY)))

    assert result.text == "const answer = 42"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 7


def test_reports_the_model_the_router_actually_served() -> None:
    """The free router picks a model per request, so the served id is what matters.

    Reporting the requested id would make every log line say `openrouter/free`,
    hiding which model produced a given answer.
    """
    result = converse(make_client(ok(COMPLETION_BODY)))

    assert result.model == "vendor/some-free-model"


def test_falls_back_to_the_requested_model_when_none_is_reported() -> None:
    body = {**COMPLETION_BODY}
    del body["model"]

    assert converse(make_client(ok(body))).model == "openrouter/free"


def test_sends_the_bearer_key_and_openai_shaped_body() -> None:
    calls: list[dict] = []
    converse(make_client(ok(COMPLETION_BODY), calls))

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["payload"]["model"] == "openrouter/free"
    assert call["payload"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert call["payload"]["max_tokens"] == 256
    assert call["timeout"] == 30.0


def test_attribution_headers_are_omitted_when_unset() -> None:
    calls: list[dict] = []
    converse(make_client(ok(COMPLETION_BODY), calls))

    assert "HTTP-Referer" not in calls[0]["headers"]
    assert "X-OpenRouter-Title" not in calls[0]["headers"]


def test_missing_api_key_is_a_configuration_error_not_a_request() -> None:
    calls: list[dict] = []

    def transport(url, body, headers, timeout):
        calls.append({"url": url})
        return ok(COMPLETION_BODY)

    client = OpenRouterClient(api_key="", timeout_seconds=30, transport=transport)

    with pytest.raises(LlmProviderNotConfiguredError):
        converse(client)
    # The guard must fire BEFORE the request, so a misconfigured deployment cannot
    # send an unauthenticated call to the provider on every user click.
    assert calls == []


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_status"),
    [
        (401, "llm_access_denied", 500),
        (403, "llm_access_denied", 500),
        (400, "llm_internal", 500),
        (502, "llm_unavailable", 503),
        (503, "llm_unavailable", 503),
        (504, "llm_unavailable", 503),
    ],
)
def test_http_failures_map_onto_the_existing_error_contract(
    status: int, expected_code: str, expected_status: int
) -> None:
    """Codes match the Bedrock mapping so the UI contract survives a provider swap."""
    client = make_client(ok({"error": {"message": "boom"}}, status=status))

    with pytest.raises(LlmServiceError) as excinfo:
        converse(client)

    assert excinfo.value.code == expected_code
    assert excinfo.value.status_code == expected_status


def test_429_is_throttling_and_forwards_retry_after() -> None:
    client = make_client(
        ok({"error": {"message": "slow down"}}, status=429, headers={"retry-after": "17"})
    )

    with pytest.raises(LlmServiceError) as excinfo:
        converse(client)

    assert excinfo.value.code == "llm_throttled"
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "17"


def test_provider_error_body_never_reaches_the_user_message() -> None:
    """Provider internals are not returned to the caller (ai-architecture.md 8.4)."""
    client = make_client(
        ok({"error": {"message": "internal host db-7 refused"}}, status=502)
    )

    with pytest.raises(LlmServiceError) as excinfo:
        converse(client)

    assert "db-7" not in excinfo.value.message


def test_provider_error_body_is_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logging stays METADATA ONLY (ai-architecture.md 8.5).

    An OpenAI-compatible error body can echo the offending request: a moderation
    rejection quotes the flagged text and a validation error can quote the message
    that triggered it. So the body may contain the user's prompt or notebook
    context, and it must not reach the logs at all — truncating it would still
    leave a prompt fragment.
    """
    captured: list[tuple[str, dict]] = []

    def fake_error(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    monkeypatch.setattr(openrouter_client.logger, "error", fake_error)

    secret_prompt = "my-notebook-secret-token-42"
    client = make_client(
        ok(
            {"error": {"message": f"moderation blocked input: {secret_prompt}"}},
            status=400,
        )
    )

    with pytest.raises(LlmServiceError):
        converse(client)

    assert captured, "the failure path must log something"
    for event, kwargs in captured:
        serialized = f"{event} {kwargs}"
        assert secret_prompt not in serialized
        assert "moderation blocked input" not in serialized
    # What SHOULD be there: the status code, which cannot carry user data.
    assert any(kwargs.get("status_code") == 400 for _, kwargs in captured)


def test_timeout_maps_to_unavailable() -> None:
    def transport(url, body, headers, timeout):
        raise TimeoutError("timed out")

    client = OpenRouterClient(api_key="k", timeout_seconds=1, transport=transport)

    with pytest.raises(LlmProviderError) as excinfo:
        converse(client)

    assert excinfo.value.code == "llm_unavailable"


def test_connection_error_maps_to_unavailable() -> None:
    def transport(url, body, headers, timeout):
        raise OSError("connection refused")

    client = OpenRouterClient(api_key="k", timeout_seconds=1, transport=transport)

    with pytest.raises(LlmProviderError) as excinfo:
        converse(client)

    assert excinfo.value.code == "llm_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {}}]},
        {"no_choices": True},
    ],
)
def test_unusable_payloads_raise_a_provider_error(body: dict) -> None:
    with pytest.raises(LlmProviderError):
        converse(make_client(ok(body)))


def test_non_json_body_raises_a_provider_error() -> None:
    client = make_client(HttpResponse(status_code=200, body="<html>502</html>", headers={}))

    with pytest.raises(LlmProviderError):
        converse(client)


def test_missing_usage_counters_degrade_to_zero() -> None:
    body = {**COMPLETION_BODY, "usage": {"prompt_tokens": None}}

    result = converse(make_client(ok(body)))

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
