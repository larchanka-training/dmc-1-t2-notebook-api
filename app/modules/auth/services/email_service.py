"""Email delivery boundary for authentication flows."""

from datetime import datetime
from typing import Protocol

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


def get_email_service(config: Settings = settings) -> EmailService:
    """Return the configured email service implementation.

    The first auth MVP uses a no-op boundary. A real provider can be added
    behind this function without changing controllers or OTP business logic.
    """
    _ = config
    return NoopEmailService()
