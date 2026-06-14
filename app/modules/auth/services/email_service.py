"""Email delivery boundary for authentication flows."""

from datetime import datetime
from typing import Protocol

import resend
from resend.http_client_requests import RequestsClient

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EmailDeliveryError(RuntimeError):
    """Raised when an OTP email cannot be delivered via the configured provider."""


class EmailService(Protocol):
    """Boundary for sending OTP emails."""

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Send an OTP code to a user email address."""


class NoopEmailService:
    """Local/dev/test email service that does not contact an external provider."""

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Pretend to send an OTP without logging the raw code."""
        _ = code
        logger.info(
            "auth.otp.email.noop",
            email=email,
            expires_at=expires_at.isoformat(),
        )


class ResendEmailService:
    """Email service that delivers OTP codes via the Resend API."""

    def __init__(self, *, api_key: str, from_email: str, request_timeout_seconds: int = 10) -> None:
        """Configure the Resend SDK with the provider API key, sender and HTTP timeout."""
        self._from_email = from_email
        resend.api_key = api_key
        # Resend's default HTTP client already times out (30s), but that is too
        # long to hold a Starlette threadpool worker hostage on a sync route.
        resend.default_http_client = RequestsClient(timeout=request_timeout_seconds)

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Send an OTP code to a user email address via Resend."""
        try:
            resend.Emails.send(
                {
                    "from": self._from_email,
                    "to": email,
                    "subject": "Your JS Notebook sign-in code",
                    "text": (
                        f"Your sign-in code is {code}. "
                        f"It expires at {expires_at.isoformat()}."
                    ),
                }
            )
        except Exception as exc:
            logger.info(
                "auth.otp.email.failed",
                email=email,
                expires_at=expires_at.isoformat(),
                error=str(exc),
            )
            raise EmailDeliveryError("Failed to send OTP email") from exc

        logger.info(
            "auth.otp.email.sent",
            email=email,
            expires_at=expires_at.isoformat(),
        )


def get_email_service(config: Settings = settings) -> EmailService:
    """Return the configured email service implementation.

    Local/dev/test environments and unrecognized ``APP_ENV`` values use a
    no-op boundary that never contacts an external provider, mirroring the
    settings validation: only production-like environments are required (and
    guaranteed by ``validate_auth_settings``) to have ``RESEND_API_KEY`` and
    ``EMAIL_FROM`` configured. This keeps the factory and the validator in
    sync — an unknown environment never reaches Resend with empty credentials.
    """
    if config.is_production_like:
        return ResendEmailService(
            api_key=config.resend_api_key,
            from_email=config.email_from,
            request_timeout_seconds=config.resend_request_timeout_seconds,
        )
    return NoopEmailService()
