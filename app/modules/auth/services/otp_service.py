"""OTP and opaque-token primitives for auth services."""

import hashlib
import hmac
import re
import secrets

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmailError(ValueError):
    """Raised when an email address fails basic validation."""


class OtpCodeService:
    """Small primitives for OTP and opaque refresh-token handling."""

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

    def hash_secret(self, value: str) -> str:
        """Hash an OTP or opaque token value for persistent storage."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def verify_secret(self, value: str, expected_hash: str) -> bool:
        """Compare a raw value with a stored hash using constant-time compare."""
        return hmac.compare_digest(self.hash_secret(value), expected_hash)
