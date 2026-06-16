"""Cleanup service for expired auth artifacts."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
)

logger = get_logger(__name__)


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

    def _cutoffs(self, now: datetime) -> tuple[datetime, datetime]:
        otp_cutoff = now - timedelta(seconds=self.otp_grace_seconds)
        session_cutoff = now - timedelta(seconds=self.retention_seconds)
        return otp_cutoff, session_cutoff

    def preview(self, *, now: datetime) -> AuthCleanupResult:
        """Return counts that ``cleanup`` would delete, without side effects."""
        otp_cutoff, session_cutoff = self._cutoffs(now)
        # TODO: batch when candidate set exceeds a few tens of thousands.
        session_ids = self.session_repository.get_cleanup_candidate_ids(
            session_cutoff
        )
        result = AuthCleanupResult(
            otps_deleted=self.otp_repository.count_expired_before(otp_cutoff),
            refresh_tokens_deleted=(
                self.refresh_token_repository.count_by_session_ids(session_ids)
            ),
            sessions_deleted=len(session_ids),
        )
        logger.info(
            "auth.cleanup.previewed",
            otps_to_delete=result.otps_deleted,
            refresh_tokens_to_delete=result.refresh_tokens_deleted,
            sessions_to_delete=result.sessions_deleted,
            otp_cutoff=otp_cutoff.isoformat(),
            session_cutoff=session_cutoff.isoformat(),
        )
        return result

    def cleanup(self, *, now: datetime) -> AuthCleanupResult:
        """Delete expired auth records according to the configured policy."""
        otp_cutoff, session_cutoff = self._cutoffs(now)

        otps_deleted = self.otp_repository.delete_expired_before(otp_cutoff)
        # TODO: batch when candidate set exceeds a few tens of thousands.
        session_ids = self.session_repository.get_cleanup_candidate_ids(
            session_cutoff
        )
        refresh_tokens_deleted = self.refresh_token_repository.delete_by_session_ids(
            session_ids
        )
        sessions_deleted = self.session_repository.delete_by_ids(session_ids)

        result = AuthCleanupResult(
            otps_deleted=otps_deleted,
            refresh_tokens_deleted=refresh_tokens_deleted,
            sessions_deleted=sessions_deleted,
        )
        logger.info(
            "auth.cleanup.completed",
            otps_deleted=result.otps_deleted,
            refresh_tokens_deleted=result.refresh_tokens_deleted,
            sessions_deleted=result.sessions_deleted,
            otp_cutoff=otp_cutoff.isoformat(),
            session_cutoff=session_cutoff.isoformat(),
        )
        return result
