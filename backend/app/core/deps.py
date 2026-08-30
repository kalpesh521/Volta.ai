"""
Shared FastAPI `Depends` providers.

Cross-cutting authn (`get_current_user`) lives here because onboarding and
future modules all need it. Auth-specific wiring lives in
`app.modules.auth.deps` and is re-exported so existing
`from app.core.deps import get_auth_service` imports keep working — FastAPI
`dependency_overrides` keys on the function object, so tests that override
`get_google_oauth_client` from this module still hit the same provider.
"""
import uuid

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import InvalidTokenError
from app.core.security import TokenType, decode_token
from app.models.user import User
from app.modules.auth.deps import (
    get_auth_provider_repository,
    get_auth_service,
    get_google_oauth_client,
    get_oauth_service,
    get_password_reset_repository,
    get_refresh_token_repository,
    get_token_service,
    get_user_repository,
)
from app.modules.auth.repositories.user_repository import UserRepository

# Shown as the green Authorize lock in /docs. Swagger reliably sends this.
# (A plain "authorization" Header parameter is often NOT sent by Swagger UI.)
_bearer_scheme = HTTPBearer(auto_error=False)

__all__ = [
    "get_user_repository",
    "get_refresh_token_repository",
    "get_auth_provider_repository",
    "get_password_reset_repository",
    "get_token_service",
    "get_auth_service",
    "get_oauth_service",
    "get_google_oauth_client",
    "get_current_user",
    "get_current_user_optional",
]


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


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_access_token: str | None = Header(
        default=None,
        alias="X-Access-Token",
        description="Optional access token (ignored when X-Ingest-Token is used)",
    ),
    user_repo: UserRepository = Depends(get_user_repository),
) -> User | None:
    """
    Same token sources as get_current_user, but missing/invalid tokens return
    None instead of 401. Used by dual-auth routes (user JWT OR ingest token).
    """
    raw_authorization = request.headers.get("authorization")
    raw_token = _extract_raw_token(credentials, raw_authorization, x_access_token)
    if not raw_token:
        return None
    try:
        payload = decode_token(raw_token)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != TokenType.ACCESS:
        return None
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
    user = await user_repo.get_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return user
