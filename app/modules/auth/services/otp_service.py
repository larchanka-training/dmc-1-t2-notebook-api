"""OTP and opaque-token primitives for auth services."""

import hashlib
import hmac
import re
import secrets

from app.core.config import Settings, settings

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmailError(ValueError):
    """Raised when an email address fails basic validation."""


class OtpCodeService:
    """Small primitives for OTP and opaque refresh-token handling."""

    def __init__(self, config: Settings = settings) -> None:
        """Create the service with server-side OTP hashing secret."""
        self._otp_hash_secret = config.otp_hash_secret.encode("utf-8")

    def normalize_email(self, email: str) -> str:
        """Normalize and validate an email address."""
        normalized = email.strip().lower()
        if not EMAIL_RE.match(normalized):
            raise InvalidEmailError("Invalid email address")
        return normalized

    def generate_otp(self) -> str:
        """Generate a six-digit numeric OTP."""
        return f"{secrets.randbelow(1_000_000):06d}"

    def generate_refresh_token(self) -> str:
        """Generate a random opaque refresh token."""
        return secrets.token_urlsafe(32)

    def hash_otp(self, value: str) -> str:
        """Hash a low-entropy OTP with a server-side secret."""
        return hmac.new(
            self._otp_hash_secret,
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_otp(self, value: str, expected_hash: str) -> bool:
        """Compare a raw OTP with a stored HMAC using constant-time compare."""
        return hmac.compare_digest(self.hash_otp(value), expected_hash)

    def hash_secret(self, value: str) -> str:
        """Hash a high-entropy opaque token for persistent storage."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def verify_secret(self, value: str, expected_hash: str) -> bool:
        """Compare an opaque token with a stored hash using constant-time compare."""
        return hmac.compare_digest(self.hash_secret(value), expected_hash)
