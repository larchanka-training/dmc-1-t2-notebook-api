"""``GET /auth/me`` validates the Bearer JWT issued at OTP verify.

This is the frontend integration contract: a valid access token resolves to
its owner; a missing/malformed/tampered token returns ``401`` in the standard
error envelope so the UI's single-flight refresh kicks in.
"""

from uuid import UUID

from fastapi.testclient import TestClient

from app.core.config import settings
from app.modules.auth.services.token_service import AccessTokenService


def _login(client: TestClient, email: str) -> dict:
    """Run the OTP flow and return the verify response (tokens + user)."""
    otp = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": email},
    ).json()["otp"]
    return client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    ).json()


def test_me_returns_token_owner(client: TestClient) -> None:
    tokens = _login(client, "alice@example.com")

    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"Authorization": f"Bearer {tokens['accessToken']}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["id"] == tokens["user"]["id"]
    # Not the dev placeholder user — proves the JWT is honoured, not X-User-Id.
    assert body["email"] != "dev@notebook.local"


def test_me_without_token_returns_401(client: TestClient) -> None:
    response = client.get(f"{settings.api_prefix}/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_me_with_malformed_token_returns_401(client: TestClient) -> None:
    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_me_with_tampered_signature_returns_401(client: TestClient) -> None:
    tokens = _login(client, "bob@example.com")
    tampered = f"{tokens['accessToken'][:-3]}xxx"

    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"Authorization": f"Bearer {tampered}"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_me_rejects_token_with_mismatched_session_owner(
    client: TestClient,
) -> None:
    alice = _login(client, "alice@example.com")
    bob = _login(client, "bob@example.com")
    bob_claims = AccessTokenService(settings).verify_access_token(bob["accessToken"])
    mismatched_token = AccessTokenService(settings).issue_access_token(
        user_id=UUID(alice["user"]["id"]),
        session_id=bob_claims.session_id,
    )

    response = client.get(
        f"{settings.api_prefix}/auth/me",
        headers={"Authorization": f"Bearer {mismatched_token}"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"
