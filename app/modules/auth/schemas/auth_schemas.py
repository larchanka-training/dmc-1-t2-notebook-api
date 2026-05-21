import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr = Field(..., description="User email", examples=["user@example.com"])
    password: str = Field(
        ..., min_length=8, max_length=128, description="Plain-text password, min 8 chars"
    )


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User email", examples=["user@example.com"])
    password: str = Field(..., description="Plain-text password")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token issued at login")


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="User identifier")
    email: EmailStr = Field(..., description="User email")
    created_at: datetime = Field(..., description="Account creation timestamp")


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT access token (Bearer)")
    refresh_token: str = Field(..., description="Opaque refresh token")
    token_type: str = Field(default="bearer", description="Token type for Authorization header")
    expires_in: int = Field(..., description="Access token lifetime in seconds")
