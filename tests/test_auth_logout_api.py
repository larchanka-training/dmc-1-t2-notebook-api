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


def test_logout_revokes_refresh_token_session(client: TestClient) -> None:
    auth_payload = request_and_verify_otp(client, "user@example.com")
    refresh_token = str(auth_payload["refreshToken"])

    response = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": refresh_token},
    )

    assert response.status_code == 204
    assert response.content == b""

    refresh_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": refresh_token},
    )

    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "refresh_revoked"


def test_logout_with_rotated_refresh_token_revokes_current_refresh_token(
    client: TestClient,
) -> None:
    auth_payload = request_and_verify_otp(client, "user@example.com")
    old_refresh_token = str(auth_payload["refreshToken"])
    rotate_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": old_refresh_token},
    )
    assert rotate_response.status_code == 200
    new_refresh_token = str(rotate_response.json()["refreshToken"])

    logout_response = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": old_refresh_token},
    )

    assert logout_response.status_code == 204

    refresh_response = client.post(
        f"{settings.api_prefix}/auth/refresh",
        json={"refreshToken": new_refresh_token},
    )

    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "refresh_revoked"


def test_logout_is_idempotent_for_unknown_or_repeated_token(
    client: TestClient,
) -> None:
    auth_payload = request_and_verify_otp(client, "user@example.com")
    refresh_token = str(auth_payload["refreshToken"])

    first_response = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": refresh_token},
    )
    second_response = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": refresh_token},
    )
    unknown_response = client.post(
        f"{settings.api_prefix}/auth/logout",
        json={"refreshToken": "missing-refresh-token"},
    )

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert unknown_response.status_code == 204


def test_logout_validation_error_uses_error_envelope(client: TestClient) -> None:
    response = client.post(f"{settings.api_prefix}/auth/logout", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
