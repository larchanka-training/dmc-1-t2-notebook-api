from datetime import UTC, datetime, timedelta
import hashlib
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.modules.auth.services import (
    AccessTokenError,
    AccessTokenService,
    InvalidEmailError,
    OtpCodeService,
)


def test_otp_service_normalizes_and_validates_email() -> None:
    service = OtpCodeService()

    assert service.normalize_email("  USER@Example.COM  ") == "user@example.com"

    with pytest.raises(InvalidEmailError):
        service.normalize_email("not-an-email")


def test_otp_service_generates_six_digit_code_and_verifies_hash() -> None:
    service = OtpCodeService()

    code = service.generate_otp()
    digest = service.hash_otp(code)

    assert len(code) == 6
    assert code.isdigit()
    assert digest != hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert service.verify_otp(code, digest) is True
    assert service.verify_otp("000000" if code != "000000" else "111111", digest) is False


def test_otp_service_generates_hashable_opaque_refresh_token() -> None:
    service = OtpCodeService()

    token = service.generate_refresh_token()
    digest = service.hash_secret(token)

    assert len(token) >= 32
    assert service.verify_secret(token, digest) is True


def test_access_token_service_issues_and_verifies_token() -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
        jwt_access_ttl_seconds=60,
    )
    service = AccessTokenService(config)
    user_id = uuid4()
    session_id = uuid4()
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)

    token = service.issue_access_token(
        user_id=user_id,
        session_id=session_id,
        now=now,
    )
    claims = service.verify_access_token(token, now=now + timedelta(seconds=30))

    assert claims.user_id == user_id
    assert claims.session_id == session_id
    assert claims.issued_at == now
    assert claims.expires_at == now + timedelta(seconds=60)


def test_access_token_service_rejects_tampered_token() -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    service = AccessTokenService(config)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    token = service.issue_access_token(
        user_id=uuid4(),
        session_id=uuid4(),
        now=now,
    )
    tampered = f"{token[:-1]}x"

    with pytest.raises(AccessTokenError, match="signature"):
        service.verify_access_token(tampered, now=now)


def test_access_token_service_rejects_expired_token() -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
        jwt_access_ttl_seconds=1,
    )
    service = AccessTokenService(config)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    token = service.issue_access_token(
        user_id=uuid4(),
        session_id=uuid4(),
        now=now,
    )

    with pytest.raises(AccessTokenError, match="expired"):
        service.verify_access_token(token, now=now + timedelta(seconds=2))
