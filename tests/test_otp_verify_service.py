from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.modules.auth.services import (
    AccessTokenService,
    OtpCodeService,
    OtpVerifyError,
    OtpVerifyRateLimitError,
    OtpVerifyService,
)


def build_service(db_session: Session, config: Settings) -> OtpVerifyService:
    return OtpVerifyService(
        otp_repository=OtpRepository(db_session),
        user_repository=UserRepository(db_session),
        session_repository=AuthSessionRepository(db_session),
        refresh_token_repository=RefreshTokenRepository(db_session),
        config=config,
    )


def test_otp_verify_service_creates_user_session_and_tokens(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
        jwt_access_ttl_seconds=60,
        jwt_refresh_ttl_seconds=3600,
    )
    code_service = OtpCodeService()
    otp_repo = OtpRepository(db_session)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    otp_repo.create(
        email="user@example.com",
        otp_hash=code_service.hash_otp("123456"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    service = build_service(db_session, config)

    result = service.verify_otp(
        email=" USER@example.com ",
        otp="123456",
        now=now + timedelta(seconds=10),
    )

    claims = AccessTokenService(config).verify_access_token(
        result.access_token,
        now=now + timedelta(seconds=20),
    )

    assert result.user.email == "user@example.com"
    assert result.session.user_id == result.user.id
    assert result.session.expires_at == now + timedelta(seconds=10 + 3600)
    assert claims.user_id == result.user.id
    assert claims.session_id == result.session.id
    assert result.refresh_token_row.session_id == result.session.id
    assert result.refresh_token_row.token_hash != result.refresh_token
    assert code_service.verify_secret(
        result.refresh_token,
        result.refresh_token_row.token_hash,
    )


def test_otp_verify_service_rejects_reused_otp(db_session: Session) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    code_service = OtpCodeService()
    otp_repo = OtpRepository(db_session)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    otp_repo.create(
        email="user@example.com",
        otp_hash=code_service.hash_otp("123456"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    service = build_service(db_session, config)

    service.verify_otp(email="user@example.com", otp="123456", now=now)

    with pytest.raises(OtpVerifyError, match="invalid_otp"):
        service.verify_otp(
            email="user@example.com",
            otp="123456",
            now=now + timedelta(seconds=1),
        )


def test_otp_verify_service_rejects_wrong_otp_without_creating_session(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    code_service = OtpCodeService()
    otp_repo = OtpRepository(db_session)
    user_repo = UserRepository(db_session)
    session_repo = AuthSessionRepository(db_session)
    refresh_repo = RefreshTokenRepository(db_session)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    otp = otp_repo.create(
        email="user@example.com",
        otp_hash=code_service.hash_otp("123456"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    service = OtpVerifyService(
        otp_repository=otp_repo,
        user_repository=user_repo,
        session_repository=session_repo,
        refresh_token_repository=refresh_repo,
        config=config,
    )

    with pytest.raises(OtpVerifyError, match="invalid_otp"):
        service.verify_otp(email="user@example.com", otp="000000", now=now)

    db_session.refresh(otp)

    assert otp.used_at is None
    assert otp.failed_attempts == 1
    assert user_repo.get_by_email("user@example.com") is None


def test_otp_verify_service_invalidates_otp_after_max_failed_attempts(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
        otp_max_attempts=2,
    )
    code_service = OtpCodeService()
    otp_repo = OtpRepository(db_session)
    user_repo = UserRepository(db_session)
    session_repo = AuthSessionRepository(db_session)
    refresh_repo = RefreshTokenRepository(db_session)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    otp = otp_repo.create(
        email="user@example.com",
        otp_hash=code_service.hash_otp("123456"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    service = OtpVerifyService(
        otp_repository=otp_repo,
        user_repository=user_repo,
        session_repository=session_repo,
        refresh_token_repository=refresh_repo,
        config=config,
    )

    with pytest.raises(OtpVerifyError, match="invalid_otp"):
        service.verify_otp(email="user@example.com", otp="000000", now=now)

    with pytest.raises(OtpVerifyRateLimitError, match="too_many_otp_attempts"):
        service.verify_otp(
            email="user@example.com",
            otp="111111",
            now=now + timedelta(seconds=1),
        )

    db_session.refresh(otp)
    used_at = otp.used_at
    if used_at is not None and used_at.tzinfo is None:
        used_at = used_at.replace(tzinfo=UTC)

    assert otp.failed_attempts == 2
    assert used_at == now + timedelta(seconds=1)
    assert user_repo.get_by_email("user@example.com") is None
