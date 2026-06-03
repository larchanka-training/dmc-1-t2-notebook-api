from fastapi.testclient import TestClient

from app.core.config import settings


def request_and_verify_otp(client: TestClient, email: str) -> dict[str, object]:
    request_response = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": email},
    )
    otp = request_response.json()["otp"]
    verify_response = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    )
    assert verify_response.status_code == 200
    return verify_response.json()


def test_refresh_rotates_refresh_token_and_returns_new_pair(
    client: TestClient,
) -> None:
    auth_payload = request_and_verify_otp(client, "user@example.com")
    old_refresh_token = str(auth_payload["refreshToken"])

    response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": old_refresh_token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accessToken"]
    assert payload["refreshToken"]
    assert payload["refreshToken"] != old_refresh_token


def test_refresh_rejects_unknown_token_with_error_envelope(
    client: TestClient,
) -> None:
    response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": "missing-refresh-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh"


def test_refresh_reuse_revokes_token_family(
    client: TestClient,
) -> None:
    auth_payload = request_and_verify_otp(client, "user@example.com")
    old_refresh_token = str(auth_payload["refreshToken"])

    first_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": old_refresh_token},
    )
    assert first_response.status_code == 200

    reuse_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": old_refresh_token},
    )
    assert reuse_response.status_code == 401
    assert reuse_response.json()["error"]["code"] == "refresh_reuse_detected"

    new_refresh_token = first_response.json()["refreshToken"]
    revoked_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": new_refresh_token},
    )
    assert revoked_response.status_code == 401
    assert revoked_response.json()["error"]["code"] == "refresh_reuse_detected"
