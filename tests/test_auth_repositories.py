from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
)


def test_otp_repository_returns_latest_active_code(db_session: Session) -> None:
    repo = OtpRepository(db_session)
    now = datetime.now(UTC)

    expired = repo.create(
        email="user@example.com",
        otp_hash="expired-hash",
        created_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=5),
    )
    first = repo.create(
        email="user@example.com",
        otp_hash="first-hash",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    latest = repo.create(
        email="user@example.com",
        otp_hash="latest-hash",
        created_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(minutes=10),
    )

    assert repo.get_latest_active_by_email("user@example.com", now) == latest
    assert repo.get_latest_active_by_email_for_update("user@example.com", now) == latest

    repo.mark_used(latest, now + timedelta(seconds=2))

    assert repo.get_latest_active_by_email("user@example.com", now) == first
    assert expired.used_at is None


def test_otp_repository_marks_active_codes_used_for_email(
    db_session: Session,
) -> None:
    repo = OtpRepository(db_session)
    now = datetime.now(UTC)
    target = repo.create(
        email="user@example.com",
        otp_hash="target-hash",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    other_email = repo.create(
        email="other@example.com",
        otp_hash="other-hash",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    updated_count = repo.mark_active_as_used_for_email(
        "user@example.com", now + timedelta(seconds=1)
    )

    db_session.refresh(target)
    db_session.refresh(other_email)

    assert updated_count == 1
    assert target.used_at is not None
    assert other_email.used_at is None


def test_session_repository_filters_revoked_and_expired_sessions(
    db_session: Session,
) -> None:
    repo = AuthSessionRepository(db_session)
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    now = datetime.now(UTC)

    active = repo.create(
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(days=1),
    )
    expired = repo.create(
        user_id=user_id,
        created_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )

    assert repo.get_by_id(active.id) == active
    assert repo.get_active_by_id(active.id, now) == active
    assert repo.get_active_by_id(expired.id, now) is None

    repo.revoke(active, now + timedelta(seconds=1))

    assert repo.get_active_by_id(active.id, now) is None


def test_refresh_token_repository_marks_rotation_reuse_and_family_revocation(
    db_session: Session,
) -> None:
    session_repo = AuthSessionRepository(db_session)
    token_repo = RefreshTokenRepository(db_session)
    user_id = UUID("00000000-0000-0000-0000-000000000001")
    family_id = uuid4()
    now = datetime.now(UTC)
    session = session_repo.create(
        user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )

    old_token = token_repo.create(
        session_id=session.id,
        token_hash="old-refresh-hash",
        family_id=family_id,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
    new_token = token_repo.create(
        session_id=session.id,
        token_hash="new-refresh-hash",
        family_id=family_id,
        created_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(days=30),
    )

    assert token_repo.get_by_hash("old-refresh-hash") == old_token
    assert token_repo.get_by_hash_for_update("old-refresh-hash") == old_token

    token_repo.mark_rotated(old_token, now + timedelta(seconds=2))
    token_repo.mark_reuse_detected(old_token, now + timedelta(seconds=3))
    revoked_count = token_repo.revoke_family(
        family_id,
        now + timedelta(seconds=4),
        reuse_detected_at=now + timedelta(seconds=4),
    )

    db_session.refresh(old_token)
    db_session.refresh(new_token)

    assert old_token.rotated_at is not None
    assert old_token.reuse_detected_at is not None
    assert old_token.revoked_at is not None
    assert new_token.revoked_at is not None
    assert new_token.reuse_detected_at is not None
    assert revoked_count == 2
