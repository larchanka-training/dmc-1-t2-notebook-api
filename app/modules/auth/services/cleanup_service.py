"""Cleanup service for expired auth artifacts."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
)


@dataclass(frozen=True)
class AuthCleanupResult:
    """Counts returned by an auth cleanup run."""

    otps_deleted: int
    refresh_tokens_deleted: int
    sessions_deleted: int


class AuthCleanupService:
    """Remove auth records that are safely past their retention windows."""

    def __init__(
        self,
        *,
        otp_repository: OtpRepository,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        otp_grace_seconds: int,
        retention_seconds: int,
    ) -> None:
        """Create an auth cleanup service."""
        self.otp_repository = otp_repository
        self.session_repository = session_repository
        self.refresh_token_repository = refresh_token_repository
        self.otp_grace_seconds = otp_grace_seconds
        self.retention_seconds = retention_seconds

    def cleanup(self, *, now: datetime) -> AuthCleanupResult:
        """Delete expired auth records according to the configured policy."""
        otp_cutoff = now - timedelta(seconds=self.otp_grace_seconds)
        session_cutoff = now - timedelta(seconds=self.retention_seconds)

        otps_deleted = self.otp_repository.delete_expired_before(otp_cutoff)
        session_ids = self.session_repository.get_cleanup_candidate_ids(
            session_cutoff
        )
        refresh_tokens_deleted = self.refresh_token_repository.delete_by_session_ids(
            session_ids
        )
        sessions_deleted = self.session_repository.delete_by_ids(session_ids)

        return AuthCleanupResult(
            otps_deleted=otps_deleted,
            refresh_tokens_deleted=refresh_tokens_deleted,
            sessions_deleted=sessions_deleted,
        )
