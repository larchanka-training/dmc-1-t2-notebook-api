"""Service orchestration for requesting email OTP codes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, settings
from app.modules.auth.models.otp import Otp
from app.modules.auth.repositories.otp_repository import OtpRepository
from app.modules.auth.services.email_service import EmailService
from app.modules.auth.services.otp_service import OtpCodeService


@dataclass(frozen=True)
class OtpRequestResult:
    """Result of an OTP request before HTTP response shaping."""

    email: str
    expires_at: datetime
    otp: Otp
    raw_code: str | None


class OtpRequestService:
    """Coordinate OTP request business logic."""

    def __init__(
        self,
        *,
        otp_repository: OtpRepository,
        email_service: EmailService,
        code_service: OtpCodeService | None = None,
        config: Settings = settings,
    ) -> None:
        """Create the service from its storage and delivery boundaries."""
        self._otp_repository = otp_repository
        self._email_service = email_service
        self._code_service = code_service or OtpCodeService()
        self._config = config

    def request_otp(
        self,
        *,
        email: str,
        now: datetime | None = None,
    ) -> OtpRequestResult:
        """Create, persist, and send a fresh OTP for an email."""
        requested_at = self._normalize_datetime(now or datetime.now(UTC))
        normalized_email = self._code_service.normalize_email(email)
        raw_code = self._code_service.generate_otp()
        otp_hash = self._code_service.hash_secret(raw_code)
        expires_at = requested_at + timedelta(seconds=self._config.otp_ttl_seconds)

        self._otp_repository.mark_active_as_used_for_email(
            normalized_email, requested_at
        )
        otp = self._otp_repository.create(
            email=normalized_email,
            otp_hash=otp_hash,
            expires_at=expires_at,
            created_at=requested_at,
        )
        self._email_service.send_otp(
            email=normalized_email,
            code=raw_code,
            expires_at=expires_at,
        )

        return OtpRequestResult(
            email=normalized_email,
            expires_at=expires_at,
            otp=otp,
            raw_code=raw_code if self._config.is_local_like else None,
        )

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
