from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.modules.auth.models import AuthSession, Otp, RefreshToken
from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
)
from app.modules.auth.services import AuthCleanupService


def _build_service(db_session: Session) -> AuthCleanupService:
    return AuthCleanupService(
        otp_repository=OtpRepository(db_session),
        session_repository=AuthSessionRepository(db_session),
        refresh_token_repository=RefreshTokenRepository(db_session),
        otp_grace_seconds=86_400,
        retention_seconds=7_776_000,
    )


def test_auth_cleanup_deletes_only_otps_past_grace(
    db_session: Session,
) -> None:
    repo = OtpRepository(db_session)
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    old_expired = repo.create(
        email="user@example.com",
        otp_hash="old-expired",
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
    )
    recent_expired = repo.create(
        email="user@example.com",
        otp_hash="recent-expired",
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    active = repo.create(
        email="user@example.com",
        otp_hash="active",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    result = _build_service(db_session).cleanup(now=now)

    assert result.otps_deleted == 1
    assert db_session.get(Otp, old_expired.id) is None
    assert db_session.get(Otp, recent_expired.id) == recent_expired
    assert db_session.get(Otp, active.id) == active


def test_auth_cleanup_deletes_only_sessions_past_retention(
    db_session: Session,
) -> None:
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)

    active = session_repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
    )
    expired_old = session_repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=100),
        expires_at=now - timedelta(days=91),
    )
    expired_recent = session_repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=91),
        expires_at=now - timedelta(days=89),
    )
    revoked_old = session_repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=100),
        expires_at=now + timedelta(days=1),
    )
    revoked_recent = session_repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=100),
        expires_at=now + timedelta(days=1),
    )
    session_repo.revoke(revoked_old, now - timedelta(days=91))
    session_repo.revoke(revoked_recent, now - timedelta(days=89))

    tokens = [
        token_repo.create(
            session_id=session.id,
            token_hash=f"refresh-{session.id}",
            family_id=uuid4(),
            created_at=session.created_at,
            expires_at=session.expires_at,
        )
        for session in [
            active,
            expired_old,
            expired_recent,
            revoked_old,
            revoked_recent,
        ]
    ]

    result = _build_service(db_session).cleanup(now=now)

    assert result.sessions_deleted == 2
    assert result.refresh_tokens_deleted == 2
    assert db_session.get(AuthSession, active.id) == active
    assert db_session.get(AuthSession, expired_old.id) is None
    assert db_session.get(AuthSession, expired_recent.id) == expired_recent
    assert db_session.get(AuthSession, revoked_old.id) is None
    assert db_session.get(AuthSession, revoked_recent.id) == revoked_recent
    assert db_session.get(RefreshToken, tokens[0].id) == tokens[0]
    assert db_session.get(RefreshToken, tokens[1].id) is None
    assert db_session.get(RefreshToken, tokens[2].id) == tokens[2]
    assert db_session.get(RefreshToken, tokens[3].id) is None
    assert db_session.get(RefreshToken, tokens[4].id) == tokens[4]


def test_auth_cleanup_is_safe_to_rerun(db_session: Session) -> None:
    repo = OtpRepository(db_session)
    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    repo.create(
        email="user@example.com",
        otp_hash="old-expired",
        created_at=now - timedelta(days=3),
        expires_at=now - timedelta(days=2),
    )

    service = _build_service(db_session)
    first = service.cleanup(now=now)
    second = service.cleanup(now=now)

    assert first.otps_deleted == 1
    assert second.otps_deleted == 0
    assert second.sessions_deleted == 0
    assert second.refresh_tokens_deleted == 0
