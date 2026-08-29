"""Email/password auth endpoints: signup, login, refresh, logout, /me, password reset."""
from fastapi import APIRouter, Depends, Request, status

from app.core.deps import get_current_user
from app.modules.auth.deps import get_auth_service, get_token_service
from app.core.rate_limit import limiter
from app.core.config import settings
from app.models.user import User
from app.modules.auth.schemas import (
    LoginRequest,
    LogoutRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    TokenResponse,
)
from app.modules.auth.user_schemas import UserCreate, UserOut
from app.modules.auth.service import AuthService
from app.modules.auth.token_service import TokenService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def signup(data: UserCreate, auth_service: AuthService = Depends(get_auth_service)) -> UserOut:
    """Creates a new password-based account. Returns 409 if the email is already taken."""
    user = await auth_service.signup(data)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_LOGIN)
async def login(
    request: Request,  # required by slowapi to read the client IP for rate limiting
    data: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """Issues an access + refresh token pair. Errors are intentionally generic (401)."""
    return await auth_service.login(data.email, data.password)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    data: RefreshRequest, token_service: TokenService = Depends(get_token_service)
) -> TokenResponse:
    """Rotates the refresh token: the old one is revoked, a new pair is issued."""
    return await token_service.rotate_refresh_token(data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: LogoutRequest, token_service: TokenService = Depends(get_token_service)
) -> None:
    """Revokes the given refresh token. Idempotent - no error if already revoked."""
    await token_service.revoke_refresh_token(data.refresh_token)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)) -> UserOut:
    """Protected endpoint - requires a valid, non-expired access token."""
    return UserOut.model_validate(current_user)


@router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.RATE_LIMIT_PASSWORD_RESET)
async def request_password_reset(
    request: Request,
    data: PasswordResetRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Always returns 204 regardless of whether the email exists (no user enumeration)."""
    await auth_service.request_password_reset(data.email)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(
    data: PasswordResetConfirm, auth_service: AuthService = Depends(get_auth_service)
) -> None:
    """Consumes a (single-use, time-limited) reset token and sets the new password."""
    await auth_service.reset_password(data.token, data.new_password)
