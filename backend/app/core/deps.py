"""
FastAPI `Depends` providers.

Wires DB session -> repositories -> services, and extracts/validates the
current user from the `Authorization: Bearer <access_token>` header. Kept
in one place so routers stay thin and swapping an implementation (e.g. the
Google OAuth client, for tests) only requires overriding here.
"""
import uuid

import jwt
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import InvalidTokenError
from app.core.security import TokenType, decode_token
from app.models.user import User
from app.repositories.auth_provider_repository import AuthProviderRepository
from app.repositories.password_reset_repository import PasswordResetRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.oauth.base import OAuthProviderClient
from app.services.oauth.google import GoogleOAuthClient
from app.services.oauth_service import OAuthService
from app.services.token_service import TokenService

# --- Repositories ---


def get_user_repository(db: AsyncSession = Depends(get_db)) -> UserRepository:
    return UserRepository(db)


def get_refresh_token_repository(db: AsyncSession = Depends(get_db)) -> RefreshTokenRepository:
    return RefreshTokenRepository(db)


def get_auth_provider_repository(db: AsyncSession = Depends(get_db)) -> AuthProviderRepository:
    return AuthProviderRepository(db)


def get_password_reset_repository(db: AsyncSession = Depends(get_db)) -> PasswordResetRepository:
    return PasswordResetRepository(db)


# --- Services ---


def get_token_service(
    refresh_token_repo: RefreshTokenRepository = Depends(get_refresh_token_repository),
) -> TokenService:
    return TokenService(refresh_token_repo)


def get_auth_service(
    user_repo: UserRepository = Depends(get_user_repository),
    token_service: TokenService = Depends(get_token_service),
    password_reset_repo: PasswordResetRepository = Depends(get_password_reset_repository),
) -> AuthService:
    return AuthService(user_repo, token_service, password_reset_repo)


def get_oauth_service(
    user_repo: UserRepository = Depends(get_user_repository),
    auth_provider_repo: AuthProviderRepository = Depends(get_auth_provider_repository),
    token_service: TokenService = Depends(get_token_service),
) -> OAuthService:
    return OAuthService(user_repo, auth_provider_repo, token_service)


def get_google_oauth_client() -> OAuthProviderClient:
    """
    Returned as the `OAuthProviderClient` interface (not the concrete Google
    class) so routers/services never depend on Google specifically - and so
    tests can override this with a fake provider client.
    """
    return GoogleOAuthClient()


# --- Current-user extraction ---


async def get_current_user(
    authorization: str | None = Header(default=None),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User:
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise InvalidTokenError("Missing or malformed Authorization header")

    raw_token = authorization.split(" ", 1)[1].strip()

    try:
        payload = decode_token(raw_token)
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc

    if payload.get("type") != TokenType.ACCESS:
        raise InvalidTokenError("Token is not an access token")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError() from exc

    user = await user_repo.get_by_id(user_id)
    if user is None or not user.is_active:
        raise InvalidTokenError()

    return user
