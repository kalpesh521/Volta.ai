"""Pydantic schemas for login, token exchange, and password reset flows."""
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """Returned by login, refresh, and Google OAuth callback - one shape for all auth paths."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class OAuthLinkConfirmRequest(BaseModel):
    """Used to complete linking a Google identity to an existing password
    account when Google did not report the email as verified."""
    link_token: str
    password: str

