"""Provider selection and the developer allowlist (roadmap Step 8d-1).

Two guarantees are locked here:

1. adding the OpenRouter adapter did NOT change the default — a deployment that
   sets nothing still gets Bedrock, so the change is inert until opted into;
2. ``LLM_ALLOWED_EMAILS`` is a real, server-side authorization control, unlike
   the UI's ``llmEnabled`` switch, which is a device-local preference.
"""

from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import app
from app.modules.llm.dependencies import get_llm_generation_service, get_rate_limiter
from app.modules.llm.schemas.llm_schemas import GenerateResponse
from app.modules.llm.services.bedrock_client import BedrockClient
from app.modules.llm.services.generation_service import build_provider
from app.modules.llm.services.openrouter_client import OpenRouterClient
from app.modules.llm.services.rate_limiter import InMemoryRateLimiter

ALLOWED_EMAIL = "dev-allowed@example.com"
OTHER_EMAIL = "someone-else@example.com"


def _login(client: TestClient, email: str) -> dict[str, str]:
    otp = client.post(
        f"{settings.api_prefix}/auth/otp/request", json={"email": email}
    ).json()["otp"]
    body = client.post(
        f"{settings.api_prefix}/auth/otp/verify", json={"email": email, "otp": otp}
    ).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


@dataclass
class FakeGenerationService:
    def generate(self, payload, user):  # type: ignore[no-untyped-def]
        return GenerateResponse(
            result_kind="code",
            content="const value = 1;",
            model="fake-model",
            request_id=uuid4(),
        )


@pytest.fixture
def llm_overrides():
    app.dependency_overrides[get_llm_generation_service] = FakeGenerationService
    app.dependency_overrides[get_rate_limiter] = lambda: InMemoryRateLimiter(20, 60)
    yield
    app.dependency_overrides.pop(get_llm_generation_service, None)
    app.dependency_overrides.pop(get_rate_limiter, None)


# ─── provider selection ──────────────────────────────────────────────────────


def test_default_provider_is_still_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    """No config change means no behaviour change — the whole point of 8d-1."""
    monkeypatch.setattr(settings, "llm_provider", "bedrock")

    provider, guard_model, generator_model = build_provider()

    assert isinstance(provider, BedrockClient)
    assert guard_model == settings.llm_bedrock_guard_model_id
    assert generator_model == settings.llm_bedrock_generator_model_id


def test_openrouter_is_selected_with_its_own_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model ids travel WITH the adapter.

    Bedrock inference-profile ids and OpenRouter slugs are not interchangeable, so
    returning them together makes a half-switched deployment (new provider, old
    model ids) impossible to express.
    """
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "llm_openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_openrouter_guard_model_id", "vendor/guard")
    monkeypatch.setattr(settings, "llm_openrouter_generator_model_id", "vendor/gen")

    provider, guard_model, generator_model = build_provider()

    assert isinstance(provider, OpenRouterClient)
    assert guard_model == "vendor/guard"
    assert generator_model == "vendor/gen"


def test_provider_id_is_case_and_whitespace_tolerant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "  OpenRouter ")
    monkeypatch.setattr(settings, "llm_openrouter_api_key", "test-key")

    provider, _, _ = build_provider()

    assert isinstance(provider, OpenRouterClient)


def test_unknown_provider_is_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="LLM_PROVIDER must be one of"):
        Settings(llm_provider="not-a-provider")


def test_openrouter_without_a_key_is_rejected_at_startup() -> None:
    """Fail on boot, not on the first user click that would 503."""
    with pytest.raises(ValidationError, match="LLM_OPENROUTER_API_KEY is required"):
        Settings(llm_provider="openrouter", llm_openrouter_api_key="")


# ─── developer allowlist ─────────────────────────────────────────────────────


def test_empty_allowlist_allows_everyone(
    client: TestClient, llm_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: an existing deployment that sets nothing is unaffected."""
    monkeypatch.setattr(settings, "llm_allowed_emails", "")
    headers = _login(client, OTHER_EMAIL)

    response = client.post(
        f"{settings.api_prefix}/llm/generate",
        json={"prompt": "make a constant"},
        headers=headers,
    )

    assert response.status_code == 200


def test_allowlisted_account_is_allowed(
    client: TestClient, llm_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_allowed_emails", f"{ALLOWED_EMAIL},other@x.io")
    headers = _login(client, ALLOWED_EMAIL)

    response = client.post(
        f"{settings.api_prefix}/llm/generate",
        json={"prompt": "make a constant"},
        headers=headers,
    )

    assert response.status_code == 200


def test_non_allowlisted_account_is_forbidden(
    client: TestClient, llm_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "llm_allowed_emails", ALLOWED_EMAIL)
    headers = _login(client, OTHER_EMAIL)

    response = client.post(
        f"{settings.api_prefix}/llm/generate",
        json={"prompt": "make a constant"},
        headers=headers,
    )

    assert response.status_code == 403
    # Envelope shape matches every other API error (ApiErrorResponse).
    assert response.json()["error"]["code"] == "llm_access_denied"


def test_denial_does_not_echo_the_allowlist_or_the_caller(
    client: TestClient, llm_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 403 body must not disclose who IS allowed, nor confirm the caller."""
    monkeypatch.setattr(settings, "llm_allowed_emails", ALLOWED_EMAIL)
    headers = _login(client, OTHER_EMAIL)

    body = client.post(
        f"{settings.api_prefix}/llm/generate",
        json={"prompt": "make a constant"},
        headers=headers,
    ).text

    assert ALLOWED_EMAIL not in body
    assert OTHER_EMAIL not in body


def test_allowlist_matching_is_case_insensitive(
    client: TestClient, llm_overrides, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The email is user-entered; a capitalisation miss would look like a bug."""
    monkeypatch.setattr(settings, "llm_allowed_emails", " Dev-Allowed@Example.COM ")
    headers = _login(client, ALLOWED_EMAIL)

    response = client.post(
        f"{settings.api_prefix}/llm/generate",
        json={"prompt": "make a constant"},
        headers=headers,
    )

    assert response.status_code == 200


def test_allowlist_runs_before_the_rate_limiter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected caller must not spend one of their own rate-limit tokens.

    The limiter is given a budget of 1: if the allowlist ran second, the refused
    request would consume it and a later allowed call would see 429 instead of 403.
    """
    monkeypatch.setattr(settings, "llm_allowed_emails", ALLOWED_EMAIL)
    limiter = InMemoryRateLimiter(1, 60)
    app.dependency_overrides[get_llm_generation_service] = FakeGenerationService
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    try:
        denied = client.post(
            f"{settings.api_prefix}/llm/generate",
            json={"prompt": "x"},
            headers=_login(client, OTHER_EMAIL),
        )
        assert denied.status_code == 403

        allowed = client.post(
            f"{settings.api_prefix}/llm/generate",
            json={"prompt": "x"},
            headers=_login(client, ALLOWED_EMAIL),
        )
        assert allowed.status_code == 200
    finally:
        app.dependency_overrides.pop(get_llm_generation_service, None)
        app.dependency_overrides.pop(get_rate_limiter, None)
