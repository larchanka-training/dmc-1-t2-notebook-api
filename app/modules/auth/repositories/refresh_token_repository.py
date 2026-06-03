"""Data-access layer for opaque refresh-token history."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.auth.models.refresh_token import RefreshToken


class RefreshTokenRepository:
    """Repository for ``users.refresh_tokens`` rows."""

    def __init__(self, db: Session) -> None:
        """Bind the repository to a request-scoped SQLAlchemy session."""
        self.db = db

    def create(
        self,
        *,
        session_id: UUID,
        token_hash: str,
        family_id: UUID,
        expires_at: datetime,
        created_at: datetime,
    ) -> RefreshToken:
        """Create a new hashed refresh-token row."""
        token = RefreshToken(
            session_id=session_id,
            token_hash=token_hash,
            family_id=family_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        self.db.add(token)
        self.db.flush()
        return token

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Fetch a refresh-token row by token hash."""
        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return self.db.execute(statement).scalar_one_or_none()

    def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        """Fetch a refresh-token row by token hash and lock it for rotation."""
        statement = (
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        return self.db.execute(statement).scalar_one_or_none()

    def mark_rotated(
        self, token: RefreshToken, rotated_at: datetime
    ) -> RefreshToken:
        """Mark a refresh token as rotated."""
        token.rotated_at = rotated_at
        self.db.add(token)
        self.db.flush()
        return token

    def revoke(self, token: RefreshToken, revoked_at: datetime) -> RefreshToken:
        """Mark a refresh token as revoked."""
        token.revoked_at = revoked_at
        self.db.add(token)
        self.db.flush()
        return token

    def mark_reuse_detected(
        self, token: RefreshToken, reuse_detected_at: datetime
    ) -> RefreshToken:
        """Mark a refresh token as reused."""
        token.reuse_detected_at = reuse_detected_at
        self.db.add(token)
        self.db.flush()
        return token

    def revoke_family(self, family_id: UUID, revoked_at: datetime) -> int:
        """Revoke all non-revoked tokens from a refresh-token family."""
        statement = (
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        result = self.db.execute(statement)
        self.db.flush()
        return result.rowcount or 0
