"""Email delivery boundary for authentication flows."""

from datetime import datetime
from typing import Protocol

import resend

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)


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

    def __init__(self, *, api_key: str, from_email: str) -> None:
        """Configure the Resend SDK with the provider API key and sender."""
        self._from_email = from_email
        resend.api_key = api_key

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Send an OTP code to a user email address via Resend."""
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
        logger.info(
            "auth.otp.email.sent",
            email=email,
            expires_at=expires_at.isoformat(),
        )


def get_email_service(config: Settings = settings) -> EmailService:
    """Return the configured email service implementation.

    Local/dev/test environments use a no-op boundary that never contacts an
    external provider. Production-like environments deliver OTP codes via
    Resend.
    """
    if config.is_local_like:
        return NoopEmailService()
    return ResendEmailService(api_key=config.resend_api_key, from_email=config.email_from)
