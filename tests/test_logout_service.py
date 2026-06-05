from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.modules.auth.repositories import AuthSessionRepository, RefreshTokenRepository
from app.modules.auth.services import LogoutService, OtpCodeService


def create_session_and_token(
    db_session: Session,
    *,
    raw_refresh_token: str,
    now: datetime,
) -> tuple[UUID, UUID]:
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)
    code_service = OtpCodeService()
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    family_id = uuid4()
    session = session_repo.create(
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
    token_repo.create(
        session_id=session.id,
        token_hash=code_service.hash_secret(raw_refresh_token),
        family_id=family_id,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
    return session.id, family_id


def build_service(db_session: Session) -> LogoutService:
    return LogoutService(
        session_repository=AuthSessionRepository(db_session),
        refresh_token_repository=RefreshTokenRepository(db_session),
    )


def test_logout_service_revokes_active_refresh_family_and_session(
    db_session: Session,
) -> None:
    now = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
    session_id, family_id = create_session_and_token(
        db_session,
        raw_refresh_token="refresh-token",
        now=now,
    )
    service = build_service(db_session)
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)

    result = service.logout(
        refresh_token="refresh-token",
        now=now + timedelta(seconds=5),
    )

    session = session_repo.get_by_id(session_id)
    token = token_repo.get_by_hash(OtpCodeService().hash_secret("refresh-token"))

    assert result.revoked is True
    assert result.session == session
    assert result.refresh_token_row == token
    assert session is not None
    assert token is not None
    assert session.revoked_at == now + timedelta(seconds=5)
    assert token.revoked_at == now + timedelta(seconds=5)
    assert token.reuse_detected_at is None
    assert token_repo.revoke_family(family_id, now + timedelta(seconds=10)) == 0


def test_logout_service_is_idempotent_for_unknown_token(
    db_session: Session,
) -> None:
    service = build_service(db_session)

    result = service.logout(refresh_token="unknown-refresh-token")

    assert result.revoked is False
    assert result.session is None
    assert result.refresh_token_row is None


def test_logout_service_is_idempotent_for_already_revoked_token(
    db_session: Session,
) -> None:
    now = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
    session_id, _ = create_session_and_token(
        db_session,
        raw_refresh_token="refresh-token",
        now=now,
    )
    service = build_service(db_session)
    first = service.logout(refresh_token="refresh-token", now=now + timedelta(seconds=1))

    second = service.logout(refresh_token="refresh-token", now=now + timedelta(seconds=2))

    session = AuthSessionRepository(db_session).get_by_id(session_id)

    assert first.revoked is True
    assert second.revoked is False
    assert second.session == session


def test_logout_service_with_rotated_token_still_revokes_session(
    db_session: Session,
) -> None:
    now = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
    session_id, family_id = create_session_and_token(
        db_session,
        raw_refresh_token="old-refresh-token",
        now=now,
    )
    code_service = OtpCodeService()
    token_repo = RefreshTokenRepository(db_session)
    old_token = token_repo.get_by_hash(
        code_service.hash_secret("old-refresh-token"),
    )
    assert old_token is not None
    token_repo.mark_rotated(old_token, now + timedelta(seconds=1))
    token_repo.create(
        session_id=session_id,
        token_hash=code_service.hash_secret("new-refresh-token"),
        family_id=family_id,
        created_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(days=30),
    )

    service = build_service(db_session)
    result = service.logout(
        refresh_token="old-refresh-token",
        now=now + timedelta(seconds=5),
    )

    session = AuthSessionRepository(db_session).get_by_id(session_id)
    new_token = token_repo.get_by_hash(
        code_service.hash_secret("new-refresh-token"),
    )

    assert result.revoked is True
    assert session is not None
    assert session.revoked_at == now + timedelta(seconds=5)
    assert old_token.revoked_at == now + timedelta(seconds=5)
    assert new_token is not None
    assert new_token.revoked_at is not None
    assert new_token.revoked_at.replace(tzinfo=UTC) == now + timedelta(seconds=5)
    assert old_token.reuse_detected_at is None
    assert new_token.reuse_detected_at is None
