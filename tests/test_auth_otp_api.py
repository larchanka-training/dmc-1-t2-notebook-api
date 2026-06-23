from fastapi.testclient import TestClient
import pytest
import resend

from app.core.config import settings


def test_otp_request_returns_dev_code(client: TestClient) -> None:
    response = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "USER@example.com"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["otp"].isdigit()
    assert len(payload["otp"]) == 6
    assert isinstance(payload["expiresAt"], int)


def test_otp_verify_returns_tokens_and_user(client: TestClient) -> None:
    request_response = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "USER@example.com"},
    )
    otp = request_response.json()["otp"]

    verify_response = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": "user@example.com", "otp": otp},
    )

    assert verify_response.status_code == 200
    payload = verify_response.json()
    assert payload["accessToken"]
    assert payload["refreshToken"]
    assert payload["user"]["email"] == "user@example.com"
    assert payload["user"]["displayName"] is None
    assert payload["user"]["roles"] == []


def test_otp_verify_rejects_wrong_code_with_error_envelope(
    client: TestClient,
) -> None:
    client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "user@example.com"},
    )

    response = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": "user@example.com", "otp": "000000"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_otp"


def test_otp_request_rate_limit_returns_error_envelope(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "otp_rate_limit_per_email", 1)
    monkeypatch.setattr(settings, "otp_rate_limit_window_seconds", 900)

    first = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "user@example.com"},
    )
    second = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "user@example.com"},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_otp_requests"


def test_otp_verify_too_many_invalid_attempts_returns_error_envelope(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "otp_max_attempts", 2)
    request_response = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "user@example.com"},
    )
    assert request_response.status_code == 200

    first = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": "user@example.com", "otp": "000000"},
    )
    second = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": "user@example.com", "otp": "111111"},
    )

    assert first.status_code == 401
    assert first.json()["error"]["code"] == "invalid_otp"
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "too_many_otp_attempts"


def test_otp_request_hides_code_in_production(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    sent_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        resend.Emails,
        "send",
        lambda params: sent_calls.append(params) or {"id": "email_123"},
    )

    response = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "user@example.com"},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert len(sent_calls) == 1
    assert sent_calls[0]["to"] == "user@example.com"
