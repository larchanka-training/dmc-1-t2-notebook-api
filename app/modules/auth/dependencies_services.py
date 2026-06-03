"""Service dependencies for auth controllers."""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.modules.auth.repositories import (
    AuthSessionRepository,
    OtpRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.modules.auth.services import (
    OtpRequestService,
    OtpVerifyService,
    RefreshTokenService,
    get_email_service,
)


def get_otp_request_service(db: Session = Depends(get_db)) -> OtpRequestService:
    """Build the OTP request service for the current request."""
    return OtpRequestService(
        otp_repository=OtpRepository(db),
        email_service=get_email_service(settings),
        config=settings,
    )


def get_otp_verify_service(db: Session = Depends(get_db)) -> OtpVerifyService:
    """Build the OTP verify service for the current request."""
    return OtpVerifyService(
        otp_repository=OtpRepository(db),
        user_repository=UserRepository(db),
        session_repository=AuthSessionRepository(db),
        refresh_token_repository=RefreshTokenRepository(db),
        config=settings,
    )


def get_refresh_token_service(db: Session = Depends(get_db)) -> RefreshTokenService:
    """Build the refresh-token rotation service for the current request."""
    return RefreshTokenService(
        session_repository=AuthSessionRepository(db),
        refresh_token_repository=RefreshTokenRepository(db),
        config=settings,
    )
