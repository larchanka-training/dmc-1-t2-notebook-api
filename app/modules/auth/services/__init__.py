"""Auth services."""

from app.modules.auth.services.email_service import (
    EmailDeliveryError,
    EmailService,
    NoopEmailService,
    ResendEmailService,
    get_email_service,
)
from app.modules.auth.services.otp_service import InvalidEmailError, OtpCodeService
from app.modules.auth.services.logout_service import LogoutResult, LogoutService
from app.modules.auth.services.otp_request_service import (
    OtpRateLimitError,
    OtpRequestResult,
    OtpRequestService,
)
from app.modules.auth.services.otp_verify_service import (
    OtpVerifyError,
    OtpVerifyRateLimitError,
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
    "EmailDeliveryError",
    "EmailService",
    "InvalidEmailError",
    "LogoutResult",
    "LogoutService",
    "NoopEmailService",
    "OtpCodeService",
    "OtpRateLimitError",
    "OtpRequestResult",
    "OtpRequestService",
    "OtpVerifyError",
    "OtpVerifyRateLimitError",
    "OtpVerifyResult",
    "OtpVerifyService",
    "RefreshTokenError",
    "RefreshTokenResult",
    "RefreshTokenService",
    "ResendEmailService",
    "get_email_service",
]
