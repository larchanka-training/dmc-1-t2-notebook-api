"""Email delivery boundary for authentication flows."""

import hashlib
import re
from datetime import datetime
from typing import Protocol

import resend
from resend.http_client_requests import RequestsClient

from app.core.config import Settings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

HTTP_STATUS_CODE_PATTERN = re.compile(r"^[1-5][0-9]{2}$")


class EmailDeliveryError(RuntimeError):
    """Raised when an OTP email cannot be delivered via the configured provider."""


def hash_email(email: str) -> str:
    """Return a 12-character SHA-256 hash of normalized email for pseudonymized log correlation."""
    clean = email.strip().lower()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]


def extract_domain(email: str) -> str:
    """Extract normalized domain from email for delivery provider diagnostics."""
    _, _, domain = email.strip().partition("@")
    return domain.strip().lower()


def extract_http_status_code(exc: Exception) -> int | None:
    """Extract and validate HTTP status code from exception attributes.

    Returns integer status code strictly in range 100..599, or None.
    Rejects non-HTTP integers, strings with leading zeros (such as 6-digit OTP codes),
    strings with keys, dicts, negative numbers, or missing attributes.
    """
    try:
        raw_code = getattr(exc, "status_code", None)
        if raw_code is None:
            raw_code = getattr(exc, "code", None)
    except Exception:
        return None

    if isinstance(raw_code, bool):
        return None
    if isinstance(raw_code, int):
        return raw_code if 100 <= raw_code <= 599 else None
    if isinstance(raw_code, str):
        cleaned = raw_code.strip()
        if HTTP_STATUS_CODE_PATTERN.fullmatch(cleaned):
            return int(cleaned)
    return None


SAFE_PROVIDER_ERROR_TYPES: frozenset[str] = frozenset({
    "application_error",
    "daily_quota_exceeded",
    "domain_not_verified",
    "internal_error",
    "internal_server_error",
    "invalid_api_key",
    "invalid_attachment",
    "invalid_from_address",
    "invalid_parameter",
    "invalid_to_address",
    "missing_api_key",
    "missing_required_field",
    "missing_required_fields",
    "monthly_quota_exceeded",
    "rate_limit_exceeded",
    "restricted_api_key",
    "validation_error",
})


def extract_provider_error(exc: Exception) -> str | None:
    """Extract machine-readable error token from provider exception if in known safe set.

    Allows only recognized Resend error enumeration tokens.
    Returns None for any unvalidated, custom, or complex values.
    """
    try:
        val = getattr(exc, "error_type", None)
    except Exception:
        return None
    if isinstance(val, str):
        candidate = val.strip().lower()
        if candidate in SAFE_PROVIDER_ERROR_TYPES:
            return candidate
    return None


class EmailService(Protocol):
    """Boundary for sending OTP emails."""

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Send an OTP code to a user email address."""


class NoopEmailService:
    """Local/dev/test email service that does not contact an external provider."""

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        """Pretend to send an OTP without logging the raw code.

        Logs the raw email in plain text intentionally for local dev/test debugging.
        """
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
        email_hash = hash_email(email)
        domain = extract_domain(email)
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
            try:
                error_type = type(exc).__name__
                error_code = extract_http_status_code(exc)
                provider_error = extract_provider_error(exc)
                logger.info(
                    "auth.otp.email.failed",
                    email_hash=email_hash,
                    domain=domain,
                    expires_at=expires_at.isoformat(),
                    error_type=error_type,
                    error_code=error_code,
                    provider_error=provider_error,
                    error=f"Failed to send OTP email: {error_type}",
                )
            except Exception as diag_err:
                try:
                    logger.warning(
                        "auth.otp.email.failed.diagnostic_error",
                        diagnostic_error_type=type(diag_err).__name__,
                    )
                except Exception:
                    pass
            raise EmailDeliveryError("Failed to send OTP email") from exc

        logger.info(
            "auth.otp.email.sent",
            email_hash=email_hash,
            domain=domain,
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
