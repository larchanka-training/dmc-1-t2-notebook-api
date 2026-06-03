"""HTTP controller for the ``auth`` module."""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.errors import ApiErrorResponse
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.dependencies_services import (
    get_otp_request_service,
    get_otp_verify_service,
)
from app.modules.auth.schemas.user_schemas import (
    CurrentUser,
    OtpRequest,
    OtpRequestDevResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from app.modules.auth.services import (
    InvalidEmailError,
    OtpRequestService,
    OtpVerifyError,
    OtpVerifyService,
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


@router.get(
    "/me",
    response_model=CurrentUser,
    summary="Get current user",
    description="Returns the current placeholder user for local development.",
)
def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return the authenticated user resolved by the dependency.

    Эндпоинт-маркер: используется фронтом, чтобы убедиться, что
    заголовок ``X-User-Id`` валиден и сервер согласен с этим
    пользователем. В будущем заменится реальной /me на базе JWT.

    Args:
        current_user: Внедряется ``Depends(get_current_user)``.

    Returns:
        Тот же :class:`CurrentUser`, без модификаций.
    """
    return current_user
