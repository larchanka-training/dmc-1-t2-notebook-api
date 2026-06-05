from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.config import settings
from app.main import app
from app.modules.auth.repositories import AuthSessionRepository, RefreshTokenRepository
from app.modules.auth.services import OtpCodeService


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


def test_refresh_reuse_revocation_survives_request_rollback(
    db_session: Session,
) -> None:
    def override_db_with_production_boundary() -> Generator[Session, None, None]:
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = override_db_with_production_boundary
    try:
        with TestClient(app) as test_client:
            auth_payload = request_and_verify_otp(test_client, "user@example.com")
            old_refresh_token = str(auth_payload["refreshToken"])

            first_response = test_client.post(
                f"{settings.api_prefix}/auth/refresh",
                json={"refreshToken": old_refresh_token},
            )
            assert first_response.status_code == 200

            reuse_response = test_client.post(
                f"{settings.api_prefix}/auth/refresh",
                json={"refreshToken": old_refresh_token},
            )
            assert reuse_response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)

    token_repo = RefreshTokenRepository(db_session)
    session_repo = AuthSessionRepository(db_session)
    old_token = token_repo.get_by_hash(OtpCodeService().hash_secret(old_refresh_token))

    assert old_token is not None
    session = session_repo.get_by_id(old_token.session_id)

    assert session is not None
    assert old_token.revoked_at is not None
    assert old_token.reuse_detected_at is not None
    assert session.revoked_at is not None
