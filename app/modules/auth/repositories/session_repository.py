"""Data-access layer for user authentication sessions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.models.auth_session import AuthSession
from app.modules.auth.models.user import User


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

    def get_active_with_user(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
        now: datetime,
    ) -> tuple[AuthSession, User] | None:
        """Return an active session and its user in one DB round-trip."""
        statement = (
            select(AuthSession, User)
            .join(User, AuthSession.user_id == User.id)
            .where(
                AuthSession.id == session_id,
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
        )
        row = self.db.execute(statement).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    def revoke(self, session: AuthSession, revoked_at: datetime) -> AuthSession:
        """Mark a session as revoked."""
        session.revoked_at = revoked_at
        self.db.add(session)
        self.db.flush()
        return session

    def get_cleanup_candidate_ids(self, cutoff: datetime) -> list[UUID]:
        """Return sessions safe to remove after auth-history retention."""
        statement = select(AuthSession.id).where(
            or_(
                AuthSession.expires_at < cutoff,
                AuthSession.revoked_at < cutoff,
            )
        )
        return list(self.db.execute(statement).scalars())

    def delete_by_ids(self, session_ids: list[UUID]) -> int:
        """Delete sessions by id."""
        if not session_ids:
            return 0
        statement = delete(AuthSession).where(AuthSession.id.in_(session_ids))
        result = self.db.execute(statement)
        self.db.flush()
        return result.rowcount or 0
