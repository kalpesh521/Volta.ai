"""
Auth-module FastAPI `Depends` providers.

Wires DB session -> repositories -> services. Kept here so the auth vertical
slice owns its own composition root. Tests override `get_google_oauth_client`
(re-exported from `app.core.deps`) without touching Google's network.
"""
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.auth.oauth.base import OAuthProviderClient
from app.modules.auth.oauth.google import GoogleOAuthClient
from app.modules.auth.oauth_service import OAuthService
from app.modules.auth.repositories.auth_provider_repository import AuthProviderRepository
from app.modules.auth.repositories.password_reset_repository import PasswordResetRepository
from app.modules.auth.repositories.refresh_token_repository import RefreshTokenRepository
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.auth.service import AuthService
from app.modules.auth.token_service import TokenService


def get_user_repository(db: AsyncSession = Depends(get_db)) -> UserRepository:
    return UserRepository(db)


def get_refresh_token_repository(db: AsyncSession = Depends(get_db)) -> RefreshTokenRepository:
    return RefreshTokenRepository(db)


def get_auth_provider_repository(db: AsyncSession = Depends(get_db)) -> AuthProviderRepository:
    return AuthProviderRepository(db)


def get_password_reset_repository(db: AsyncSession = Depends(get_db)) -> PasswordResetRepository:
    return PasswordResetRepository(db)


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
