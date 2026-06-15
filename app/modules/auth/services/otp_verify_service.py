"""Service orchestration for verifying OTP codes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.config import Settings, settings
from app.modules.auth.models.auth_session import AuthSession
from app.modules.auth.models.refresh_token import RefreshToken
from app.modules.auth.models.user import User
from app.modules.auth.repositories.otp_repository import OtpRepository
from app.modules.auth.repositories.refresh_token_repository import (
    RefreshTokenRepository,
)
from app.modules.auth.repositories.session_repository import AuthSessionRepository
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.auth.services.otp_service import OtpCodeService
from app.modules.auth.services.token_service import AccessTokenService


class OtpVerifyError(ValueError):
    """Raised when OTP verification cannot authenticate the user."""


class OtpVerifyRateLimitError(OtpVerifyError):
    """Raised when an OTP has too many failed verification attempts."""


@dataclass(frozen=True)
class OtpVerifyResult:
    """Result of a successful OTP verification before HTTP response shaping."""

    access_token: str
    refresh_token: str
    user: User
    session: AuthSession
    refresh_token_row: RefreshToken


class OtpVerifyService:
    """Coordinate OTP verification and token/session creation."""

    def __init__(
        self,
        *,
        otp_repository: OtpRepository,
        user_repository: UserRepository,
        session_repository: AuthSessionRepository,
        refresh_token_repository: RefreshTokenRepository,
        code_service: OtpCodeService | None = None,
        access_token_service: AccessTokenService | None = None,
        config: Settings = settings,
    ) -> None:
        """Create the service from storage and token boundaries."""
        self._otp_repository = otp_repository
        self._user_repository = user_repository
        self._session_repository = session_repository
        self._refresh_token_repository = refresh_token_repository
        self._code_service = code_service or OtpCodeService()
        self._access_token_service = access_token_service or AccessTokenService(config)
        self._config = config

    def verify_otp(
        self,
        *,
        email: str,
        otp: str,
        now: datetime | None = None,
    ) -> OtpVerifyResult:
        """Verify an OTP and issue session tokens."""
        verified_at = self._normalize_datetime(now or datetime.now(UTC))
        normalized_email = self._code_service.normalize_email(email)
        otp_row = self._otp_repository.get_latest_active_by_email_for_update(
            normalized_email,
            verified_at,
        )
        if otp_row is None:
            raise OtpVerifyError("invalid_otp")
        if otp_row.failed_attempts >= self._config.otp_max_attempts:
            self._otp_repository.mark_used(otp_row, verified_at)
            raise OtpVerifyRateLimitError("too_many_otp_attempts")
        if not self._code_service.verify_otp(otp, otp_row.otp_hash):
            self._otp_repository.increment_failed_attempts(otp_row)
            if otp_row.failed_attempts >= self._config.otp_max_attempts:
                self._otp_repository.mark_used(otp_row, verified_at)
                raise OtpVerifyRateLimitError("too_many_otp_attempts")
            raise OtpVerifyError("invalid_otp")

        self._otp_repository.mark_used(otp_row, verified_at)
        user = self._user_repository.get_or_create_by_email(
            email=normalized_email,
            created_at=verified_at,
        )
        session_expires_at = verified_at + timedelta(
            seconds=self._config.jwt_refresh_ttl_seconds
        )
        session = self._session_repository.create(
            user_id=user.id,
            created_at=verified_at,
            expires_at=session_expires_at,
        )
        refresh_token = self._code_service.generate_refresh_token()
        refresh_token_row = self._refresh_token_repository.create(
            session_id=session.id,
            token_hash=self._code_service.hash_secret(refresh_token),
            family_id=uuid4(),
            created_at=verified_at,
            expires_at=session_expires_at,
        )
        access_token = self._access_token_service.issue_access_token(
            user_id=user.id,
            session_id=session.id,
            now=verified_at,
        )
        return OtpVerifyResult(
            access_token=access_token,
            refresh_token=refresh_token,
            user=user,
            session=session,
            refresh_token_row=refresh_token_row,
        )

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
