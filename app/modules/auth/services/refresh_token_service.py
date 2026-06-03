"""Service orchestration for refresh-token rotation."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings, settings
from app.modules.auth.models.auth_session import AuthSession
from app.modules.auth.models.refresh_token import RefreshToken
from app.modules.auth.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.modules.auth.repositories.session_repository import AuthSessionRepository
from app.modules.auth.services.otp_service import OtpCodeService
from app.modules.auth.services.token_service import AccessTokenService


class RefreshTokenError(ValueError):
    """Raised when refresh-token rotation cannot proceed."""


@dataclass(frozen=True)
class RefreshTokenResult:
    """Result of a successful refresh-token rotation."""

    access_token: str
    refresh_token: str
    session: AuthSession
    refresh_token_row: RefreshToken


class RefreshTokenService:
    """Coordinate refresh-token rotation and reuse detection."""

    def __init__(
        self,
        *,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        code_service: OtpCodeService | None = None,
        access_token_service: AccessTokenService | None = None,
        config: Settings = settings,
    ) -> None:
        """Create the service from storage and token boundaries."""
        self._session_repository = session_repository
        self._refresh_token_repository = refresh_token_repository
        self._code_service = code_service or OtpCodeService()
        self._access_token_service = access_token_service or AccessTokenService(config)

    def refresh(
        self,
        *,
        refresh_token: str,
        now: datetime | None = None,
    ) -> RefreshTokenResult:
        """Rotate a refresh token and issue new access credentials."""
        refreshed_at = self._normalize_datetime(now or datetime.now(UTC))
        token_hash = self._code_service.hash_secret(refresh_token)
        token_row = self._refresh_token_repository.get_by_hash_for_update(token_hash)
        if token_row is None:
            raise RefreshTokenError("invalid_refresh")

        session = self._session_repository.get_by_id(token_row.session_id)
        if session is None:
            raise RefreshTokenError("invalid_refresh")

        if token_row.rotated_at is not None or token_row.revoked_at is not None:
            self._refresh_token_repository.mark_reuse_detected(
                token_row,
                refreshed_at,
            )
            self._refresh_token_repository.revoke_family(
                token_row.family_id,
                refreshed_at,
            )
            self._session_repository.revoke(session, refreshed_at)
            raise RefreshTokenError("refresh_reuse_detected")

        if session.revoked_at is not None:
            raise RefreshTokenError("refresh_revoked")
        if self._is_expired(session.expires_at, refreshed_at) or self._is_expired(
            token_row.expires_at,
            refreshed_at,
        ):
            raise RefreshTokenError("refresh_expired")

        new_refresh_token = self._code_service.generate_refresh_token()
        new_token_row = self._refresh_token_repository.create(
            session_id=session.id,
            token_hash=self._code_service.hash_secret(new_refresh_token),
            family_id=token_row.family_id,
            created_at=refreshed_at,
            expires_at=session.expires_at,
        )
        self._refresh_token_repository.mark_rotated(token_row, refreshed_at)
        access_token = self._access_token_service.issue_access_token(
            user_id=session.user_id,
            session_id=session.id,
            now=refreshed_at,
        )
        return RefreshTokenResult(
            access_token=access_token,
            refresh_token=new_refresh_token,
            session=session,
            refresh_token_row=new_token_row,
        )

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _is_expired(self, expires_at: datetime, now: datetime) -> bool:
        return self._normalize_datetime(expires_at) <= now
