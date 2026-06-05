from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.auth.repositories import AuthSessionRepository, RefreshTokenRepository
from app.modules.auth.services import (
    AccessTokenService,
    OtpCodeService,
    RefreshTokenError,
    RefreshTokenService,
)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_service(db_session: Session, config: Settings) -> RefreshTokenService:
    return RefreshTokenService(
        session_repository=AuthSessionRepository(db_session),
        refresh_token_repository=RefreshTokenRepository(db_session),
        config=config,
    )


def create_session_and_token(
    db_session: Session,
    *,
    raw_refresh_token: str,
    now: datetime,
    expires_at: datetime,
) -> tuple[UUID, UUID]:
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)
    code_service = OtpCodeService()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    family_id = uuid4()
    session = session_repo.create(
        user_id=user_id,
        created_at=now,
        expires_at=expires_at,
    )
    token_repo.create(
        session_id=session.id,
        token_hash=code_service.hash_secret(raw_refresh_token),
        family_id=family_id,
        created_at=now,
        expires_at=expires_at,
    )
    return session.id, family_id


def test_refresh_token_service_rotates_refresh_token_and_issues_access(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
        jwt_access_ttl_seconds=60,
    )
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    session_id, family_id = create_session_and_token(
        db_session,
        raw_refresh_token="old-refresh-token",
        now=now,
        expires_at=now + timedelta(days=30),
    )
    service = build_service(db_session, config)
    token_repo = RefreshTokenRepository(db_session)

    result = service.refresh(
        refresh_token="old-refresh-token",
        now=now + timedelta(seconds=10),
    )

    old_token = token_repo.get_by_hash(OtpCodeService().hash_secret("old-refresh-token"))
    claims = AccessTokenService(config).verify_access_token(
        result.access_token,
        now=now + timedelta(seconds=20),
    )

    assert old_token is not None
    assert old_token.rotated_at is not None
    assert as_utc(old_token.rotated_at) == now + timedelta(seconds=10)
    assert result.refresh_token != "old-refresh-token"
    assert result.refresh_token_row.session_id == session_id
    assert result.refresh_token_row.family_id == family_id
    assert result.refresh_token_row.token_hash != result.refresh_token
    assert claims.session_id == session_id


def test_refresh_token_service_rejects_unknown_refresh_token(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    service = build_service(db_session, config)

    with pytest.raises(RefreshTokenError, match="invalid_refresh"):
        service.refresh(refresh_token="missing-refresh-token")


def test_refresh_token_service_rejects_expired_session(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    create_session_and_token(
        db_session,
        raw_refresh_token="expired-refresh-token",
        now=now - timedelta(days=31),
        expires_at=now - timedelta(seconds=1),
    )
    service = build_service(db_session, config)

    with pytest.raises(RefreshTokenError, match="refresh_expired"):
        service.refresh(refresh_token="expired-refresh-token", now=now)


def test_refresh_token_service_detects_reuse_and_revokes_family(
    db_session: Session,
) -> None:
    config = Settings(
        _env_file=None,
        jwt_secret="test-secret-value-at-least-32-chars",
    )
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    session_id, family_id = create_session_and_token(
        db_session,
        raw_refresh_token="old-refresh-token",
        now=now,
        expires_at=now + timedelta(days=30),
    )
    service = build_service(db_session, config)
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)

    service.refresh(refresh_token="old-refresh-token", now=now + timedelta(seconds=1))

    with pytest.raises(RefreshTokenError, match="refresh_reuse_detected"):
        service.refresh(refresh_token="old-refresh-token", now=now + timedelta(seconds=2))

    old_token = token_repo.get_by_hash(OtpCodeService().hash_secret("old-refresh-token"))
    session = session_repo.get_by_id(session_id)

    assert old_token is not None
    assert session is not None
    assert old_token.reuse_detected_at is not None
    assert session.revoked_at is not None
    assert as_utc(old_token.reuse_detected_at) == now + timedelta(seconds=2)
    assert as_utc(session.revoked_at) == now + timedelta(seconds=2)
    assert token_repo.revoke_family(family_id, now + timedelta(seconds=3)) == 0
