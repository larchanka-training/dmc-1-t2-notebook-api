"""Data-access layer for user authentication sessions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.auth.models.auth_session import AuthSession


class AuthSessionRepository:
    """Repository for ``users.sessions`` rows."""

    def __init__(self, db: Session) -> None:
        """Bind the repository to a request-scoped SQLAlchemy session."""
        self.db = db

    def create(
        self,
        *,
        user_id: UUID,
        expires_at: datetime,
        created_at: datetime,
    ) -> AuthSession:
        """Create a new active auth session."""
        session = AuthSession(
            user_id=user_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        self.db.add(session)
        self.db.flush()
        return session

    def get_by_id(self, session_id: UUID) -> AuthSession | None:
        """Fetch a session by primary key."""
        return self.db.get(AuthSession, session_id)

    def get_active_by_id(
        self, session_id: UUID, now: datetime
    ) -> AuthSession | None:
        """Return an unexpired, non-revoked session by id."""
        statement = select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        return self.db.execute(statement).scalar_one_or_none()

    def revoke(self, session: AuthSession, revoked_at: datetime) -> AuthSession:
        """Mark a session as revoked."""
        session.revoked_at = revoked_at
        self.db.add(session)
        self.db.flush()
        return session
