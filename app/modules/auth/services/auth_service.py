import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.auth.models import Session, User
from app.modules.auth.services.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

logger = get_logger(__name__)


class AuthError(Exception):
    """Base class for auth domain errors."""


class EmailAlreadyExists(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class InvalidRefreshToken(AuthError):
    pass


def register_user(db: DbSession, email: str, password: str) -> User:
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise EmailAlreadyExists(email)

    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("auth.register", user_id=str(user.id))
    return user


def authenticate_user(db: DbSession, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentials()
    return user


def issue_tokens(db: DbSession, user: User) -> tuple[str, str, int]:
    refresh_token = generate_refresh_token()
    session = Session(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh_token),
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    db.commit()

    access_token = create_access_token(user.id)
    return access_token, refresh_token, settings.token_ttl_seconds


def refresh_tokens(db: DbSession, refresh_token: str) -> tuple[str, str, int]:
    session = db.scalar(
        select(Session).where(
            Session.refresh_token_hash == hash_refresh_token(refresh_token)
        )
    )
    if session is None or session.revoked:
        raise InvalidRefreshToken()

    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise InvalidRefreshToken()

    session.revoked = True
    user = db.get(User, session.user_id)
    if user is None:
        raise InvalidRefreshToken()
    return issue_tokens(db, user)


def revoke_session(db: DbSession, refresh_token: str) -> None:
    session = db.scalar(
        select(Session).where(
            Session.refresh_token_hash == hash_refresh_token(refresh_token)
        )
    )
    if session is not None and not session.revoked:
        session.revoked = True
        db.commit()


def get_user_by_id(db: DbSession, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)
