"""JWT access-token primitives for authentication."""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.config import Settings, settings

JWT_ALGORITHM = "HS256"
JWT_TYPE = "JWT"


class AccessTokenError(ValueError):
    """Raised when an access token cannot be trusted."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """Verified access-token claims used by auth dependencies."""

    user_id: UUID
    session_id: UUID
    issued_at: datetime
    expires_at: datetime


class AccessTokenService:
    """Issue and verify HS256 JWT access tokens."""

    def __init__(self, config: Settings = settings) -> None:
        """Create a token service from runtime settings."""
        self._secret = config.jwt_secret.encode("utf-8")
        self._ttl = config.jwt_access_ttl_seconds

    def issue_access_token(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        now: datetime | None = None,
    ) -> str:
        """Issue a signed access token for a user session."""
        issued_at = self._normalize_datetime(now or datetime.now(UTC))
        expires_at = issued_at + timedelta(seconds=self._ttl)
        header = {"alg": JWT_ALGORITHM, "typ": JWT_TYPE}
        payload = {
            "sub": str(user_id),
            "sessionId": str(session_id),
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        signing_input = ".".join(
            [
                self._base64url_json(header),
                self._base64url_json(payload),
            ]
        )
        signature = self._sign(signing_input)
        return f"{signing_input}.{signature}"

    def verify_access_token(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> AccessTokenClaims:
        """Verify a signed access token and return trusted claims."""
        parts = token.split(".")
        if len(parts) != 3:
            raise AccessTokenError("Invalid access token format")

        signing_input = ".".join(parts[:2])
        expected_signature = self._sign(signing_input)
        if not hmac.compare_digest(parts[2], expected_signature):
            raise AccessTokenError("Invalid access token signature")

        header = self._decode_json(parts[0])
        if header.get("alg") != JWT_ALGORITHM or header.get("typ") != JWT_TYPE:
            raise AccessTokenError("Unsupported access token header")

        payload = self._decode_json(parts[1])
        issued_at = self._timestamp_to_datetime(payload.get("iat"))
        expires_at = self._timestamp_to_datetime(payload.get("exp"))
        if expires_at <= self._normalize_datetime(now or datetime.now(UTC)):
            raise AccessTokenError("Access token expired")

        try:
            user_id = UUID(str(payload["sub"]))
            session_id = UUID(str(payload["sessionId"]))
        except (KeyError, ValueError) as exc:
            raise AccessTokenError("Invalid access token claims") from exc

        return AccessTokenClaims(
            user_id=user_id,
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def _sign(self, signing_input: str) -> str:
        digest = hmac.new(
            self._secret,
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return self._base64url_bytes(digest)

    def _base64url_json(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self._base64url_bytes(raw)

    def _base64url_bytes(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _decode_json(self, value: str) -> dict[str, Any]:
        try:
            decoded = self._base64url_decode(value)
            payload = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AccessTokenError("Invalid access token encoding") from exc
        if not isinstance(payload, dict):
            raise AccessTokenError("Invalid access token payload")
        return payload

    def _base64url_decode(self, value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(f"{value}{padding}")

    def _timestamp_to_datetime(self, value: object) -> datetime:
        if not isinstance(value, int):
            raise AccessTokenError("Invalid access token timestamp")
        return datetime.fromtimestamp(value, UTC)

    def _normalize_datetime(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
