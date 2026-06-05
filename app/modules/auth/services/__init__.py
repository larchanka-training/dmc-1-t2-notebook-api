"""Auth services."""

from app.modules.auth.services.email_service import (
    EmailService,
    NoopEmailService,
    get_email_service,
)
from app.modules.auth.services.otp_service import InvalidEmailError, OtpCodeService
from app.modules.auth.services.otp_request_service import (
    OtpRequestResult,
    OtpRequestService,
)
from app.modules.auth.services.otp_verify_service import (
    OtpVerifyError,
    OtpVerifyResult,
    OtpVerifyService,
)
from app.modules.auth.services.refresh_token_service import (
    RefreshTokenError,
    RefreshTokenResult,
    RefreshTokenService,
)
from app.modules.auth.services.token_service import (
    AccessTokenClaims,
    AccessTokenError,
    AccessTokenService,
)

__all__ = [
    "AccessTokenClaims",
    "AccessTokenError",
    "AccessTokenService",
    "EmailService",
    "InvalidEmailError",
    "NoopEmailService",
    "OtpCodeService",
    "OtpRequestResult",
    "OtpRequestService",
    "OtpVerifyError",
    "OtpVerifyResult",
    "OtpVerifyService",
    "RefreshTokenError",
    "RefreshTokenResult",
    "RefreshTokenService",
    "get_email_service",
]
