from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as DbSession

from app.core.db import get_db
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.modules.auth.services import (
    EmailAlreadyExists,
    InvalidCredentials,
    InvalidRefreshToken,
    authenticate_user,
    issue_tokens,
    refresh_tokens,
    register_user,
    revoke_session,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _token_response(tokens: tuple[str, str, int]) -> TokenResponse:
    access, refresh, expires_in = tokens
    return TokenResponse(access_token=access, refresh_token=refresh, expires_in=expires_in)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="Creates an account with email and password. Email must be unique.",
)
def register(payload: RegisterRequest, db: DbSession = Depends(get_db)) -> User:
    try:
        return register_user(db, payload.email, payload.password)
    except EmailAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and obtain tokens",
    description="Verifies credentials and returns a JWT access token and a refresh token.",
)
def login(payload: LoginRequest, db: DbSession = Depends(get_db)) -> TokenResponse:
    try:
        user = authenticate_user(db, payload.email, payload.password)
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    return _token_response(issue_tokens(db, user))


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for new tokens",
    description="Rotates the refresh token: the old one is revoked and a new pair is issued.",
)
def refresh(payload: RefreshRequest, db: DbSession = Depends(get_db)) -> TokenResponse:
    try:
        return _token_response(refresh_tokens(db, payload.refresh_token))
    except InvalidRefreshToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired or revoked refresh token",
        )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
    description="Invalidates the given refresh token so it can no longer be used.",
)
def logout(payload: RefreshRequest, db: DbSession = Depends(get_db)) -> None:
    revoke_session(db, payload.refresh_token)


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get the current authenticated user",
    description="Returns the profile of the user identified by the Bearer access token.",
)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
