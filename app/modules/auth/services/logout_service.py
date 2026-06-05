"""Service orchestration for refresh-token based logout."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.modules.auth.models.auth_session import AuthSession
from app.modules.auth.models.refresh_token import RefreshToken
from app.modules.auth.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.modules.auth.repositories.session_repository import AuthSessionRepository
from app.modules.auth.services.otp_service import OtpCodeService


@dataclass(frozen=True)
class LogoutResult:
    """Internal result of an idempotent logout request.

    The HTTP controller always returns 204. These fields exist so service tests
    can assert which storage rows were found and whether a live session changed.
    """

    session: AuthSession | None
    refresh_token_row: RefreshToken | None
    revoked: bool


class LogoutService:
    """Revoke the session identified by an opaque refresh token."""

    def __init__(
        self,
        *,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        code_service: OtpCodeService | None = None,
    ) -> None:
        """Create the service from storage boundaries."""
        self._session_repository = session_repository
        self._refresh_token_repository = refresh_token_repository
        self._code_service = code_service or OtpCodeService()

    def logout(
        self,
        *,
        refresh_token: str,
        now: datetime | None = None,
    ) -> LogoutResult:
        """Revoke the active refresh-token family and session if known."""
        logged_out_at = self._normalize_datetime(now or datetime.now(UTC))
        token_hash = self._code_service.hash_secret(refresh_token)
        token_row = self._refresh_token_repository.get_by_hash_for_update(token_hash)
        if token_row is None:
            return LogoutResult(
                session=None,
                refresh_token_row=None,
                revoked=False,
            )

        session = self._session_repository.get_by_id(token_row.session_id)
        session_was_active = session is not None and session.revoked_at is None
        if session_was_active:
            self._refresh_token_repository.revoke_family(
                token_row.family_id,
                logged_out_at,
            )
            self._session_repository.revoke(session, logged_out_at)

        return LogoutResult(
            session=session,
            refresh_token_row=token_row,
            revoked=session_was_active,
        )

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
