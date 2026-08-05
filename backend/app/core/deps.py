"""
FastAPI `Depends` providers.

Wires DB session -> repositories -> services, and extracts/validates the
current user from the access token. Kept in one place so routers stay thin
and swapping an implementation (e.g. the Google OAuth client, for tests)
only requires overriding here.
"""
import uuid

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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

# Shown as the green Authorize lock in /docs. Swagger reliably sends this.
# (A plain "authorization" Header parameter is often NOT sent by Swagger UI.)
_bearer_scheme = HTTPBearer(auto_error=False)

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


def _extract_raw_token(
    credentials: HTTPAuthorizationCredentials | None,
    authorization_header: str | None,
    x_access_token: str | None,
) -> str | None:
    """Pull the JWT from any of the supported places (first match wins)."""
    if credentials is not None and credentials.scheme.lower() == "bearer":
        token = credentials.credentials.strip()
        if token:
            return token

    if authorization_header:
        value = authorization_header.strip()
        if value.lower().startswith("bearer "):
            return value.split(" ", 1)[1].strip()
        if value:
            return value

    # Swagger reliably sends custom headers; Authorization is often blocked.
    if x_access_token and x_access_token.strip():
        return x_access_token.strip()

    return None


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_access_token: str | None = Header(
        default=None,
        alias="X-Access-Token",
        description="Paste access_token here if Authorize/Bearer does not work in Swagger",
    ),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User:
    """
    Token sources (any one is enough):
      1. Authorize lock in /docs  ->  Authorization: Bearer <token>
      2. Header X-Access-Token: <token>   (works in Swagger Parameters)
      3. Authorization header (curl/Postman)
    """
    # Also read raw header in case Depends/HTTPBearer missed a non-Bearer value.
    raw_authorization = request.headers.get("authorization")

    raw_token = _extract_raw_token(credentials, raw_authorization, x_access_token)
    if not raw_token:
        raise InvalidTokenError(
            "Missing access token. In Swagger: click Authorize (top) and paste "
            "access_token, OR fill the X-Access-Token header parameter."
        )

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
