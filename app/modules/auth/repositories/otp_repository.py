"""Data-access layer for one-time email login codes."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.auth.models.otp import Otp


class OtpRepository:
    """Repository for ``users.otps`` rows."""

    def __init__(self, db: Session) -> None:
        """Bind the repository to a request-scoped SQLAlchemy session."""
        self.db = db

    def create(
        self,
        *,
        email: str,
        otp_hash: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> Otp:
        """Create a new OTP row with a hashed code."""
        otp = Otp(
            email=email,
            otp_hash=otp_hash,
            expires_at=expires_at,
            created_at=created_at,
        )
        self.db.add(otp)
        self.db.flush()
        return otp

    def get_latest_active_by_email(self, email: str, now: datetime) -> Otp | None:
        """Return the newest unused, unexpired OTP for an email."""
        statement = (
            select(Otp)
            .where(
                Otp.email == email,
                Otp.used_at.is_(None),
                Otp.expires_at > now,
            )
            .order_by(Otp.expires_at.desc(), Otp.created_at.desc())
            .limit(1)
        )
        return self.db.execute(statement).scalar_one_or_none()

    def get_latest_active_by_email_for_update(
        self, email: str, now: datetime
    ) -> Otp | None:
        """Return and lock the newest active OTP for verification."""
        statement = (
            select(Otp)
            .where(
                Otp.email == email,
                Otp.used_at.is_(None),
                Otp.expires_at > now,
            )
            .order_by(Otp.expires_at.desc(), Otp.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return self.db.execute(statement).scalar_one_or_none()

    def mark_active_as_used_for_email(self, email: str, used_at: datetime) -> int:
        """Mark all currently unused OTP rows for an email as used."""
        statement = (
            update(Otp)
            .where(
                Otp.email == email,
                Otp.used_at.is_(None),
            )
            .values(used_at=used_at)
        )
        result = self.db.execute(statement)
        self.db.flush()
        return result.rowcount or 0

    def mark_used(self, otp: Otp, used_at: datetime) -> Otp:
        """Mark an OTP as consumed."""
        otp.used_at = used_at
        self.db.add(otp)
        self.db.flush()
        return otp
