"""HTTP controller for the ``auth`` module."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ApiErrorResponse
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.dependencies_services import (
    get_logout_service,
    get_refresh_token_service,
    get_otp_request_service,
    get_otp_verify_service,
)
from app.modules.auth.schemas.user_schemas import (
    CurrentUser,
    LogoutRequest,
    OtpRequest,
    OtpRequestDevResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
    RefreshRequest,
    RefreshResponse,
)
from app.modules.auth.services import (
    InvalidEmailError,
    LogoutService,
    OtpRequestService,
    OtpVerifyError,
    OtpVerifyService,
    RefreshTokenError,
    RefreshTokenService,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/otp/request",
    response_model=OtpRequestDevResponse,
    responses={
        204: {"description": "OTP sent without response body"},
        400: {"model": ApiErrorResponse, "description": "Invalid email"},
        422: {"model": ApiErrorResponse, "description": "Validation error"},
    },
    summary="Request email OTP",
)
def request_otp(
    payload: OtpRequest,
    service: OtpRequestService = Depends(get_otp_request_service),
) -> OtpRequestDevResponse | Response:
    """Create and send an OTP for the provided email address."""
    try:
        result = service.request_otp(email=str(payload.email))
    except InvalidEmailError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_email", "message": "Invalid email"},
        ) from exc

    if result.raw_code is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return OtpRequestDevResponse(
        otp=result.raw_code,
        expiresAt=int(result.expires_at.timestamp() * 1000),
    )


@router.post(
    "/otp/verify",
    response_model=OtpVerifyResponse,
    responses={
        400: {"model": ApiErrorResponse, "description": "Invalid email"},
        401: {"model": ApiErrorResponse, "description": "Invalid or expired OTP"},
        422: {"model": ApiErrorResponse, "description": "Validation error"},
    },
    summary="Verify email OTP",
)
def verify_otp(
    payload: OtpVerifyRequest,
    service: OtpVerifyService = Depends(get_otp_verify_service),
) -> OtpVerifyResponse:
    """Verify an OTP and return access/refresh tokens."""
    try:
        result = service.verify_otp(email=str(payload.email), otp=payload.otp)
    except InvalidEmailError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_email", "message": "Invalid email"},
        ) from exc
    except OtpVerifyError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": str(exc), "message": "Invalid or expired OTP"},
        ) from exc

    return OtpVerifyResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        user=CurrentUser(
            id=result.user.id,
            email=result.user.email,
            display_name=result.user.display_name,
            roles=[],
        ),
    )


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    responses={
        401: {"model": ApiErrorResponse, "description": "Invalid refresh token"},
        422: {"model": ApiErrorResponse, "description": "Validation error"},
    },
    summary="Refresh auth tokens",
)
def refresh_tokens(
    payload: RefreshRequest,
    db: Session = Depends(get_db),
    service: RefreshTokenService = Depends(get_refresh_token_service),
) -> RefreshResponse:
    """Rotate a refresh token and return a new token pair."""
    try:
        result = service.refresh(refresh_token=payload.refresh_token)
    except RefreshTokenError as exc:
        if str(exc) == "refresh_reuse_detected":
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": str(exc), "message": "Invalid refresh token"},
        ) from exc

    return RefreshResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Session revoked or already inactive"},
        422: {"model": ApiErrorResponse, "description": "Validation error"},
    },
    summary="Logout current refresh-token session",
)
def logout(
    payload: LogoutRequest,
    service: LogoutService = Depends(get_logout_service),
) -> Response:
    """Revoke the session identified by the refresh token."""
    service.logout(refresh_token=payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=CurrentUser,
    responses={
        401: {"model": ApiErrorResponse, "description": "Missing or invalid access token"},
    },
    summary="Get current user",
    description="Returns the user owning the Bearer access token.",
)
def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return the user resolved from the Bearer JWT access token.

    Используется фронтом при загрузке/перезагрузке для восстановления
    сессии: валидный токен → текущий пользователь; отсутствующий или
    просроченный → ``401``, что запускает single-flight refresh.

    Args:
        current_user: Внедряется ``Depends(get_current_user)``.

    Returns:
        Тот же :class:`CurrentUser`, без модификаций.
    """
    return current_user
