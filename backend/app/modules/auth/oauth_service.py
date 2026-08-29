"""
Provider-agnostic OAuth login/linking orchestration.

This is where the account-linking policy lives:
  1. Provider identity already linked         -> log the linked user in.
  2. No link, but email matches existing user:
       a. Provider confirms email_verified    -> auto-link, log in.
       b. Provider does NOT confirm email     -> refuse to auto-link; issue a
          short-lived `link_token` and require the caller to prove ownership
          of the existing account via its password (`confirm_link`).
  3. No existing user at all                  -> create a new, password-less
       account and link the provider identity to it.
Nothing here references "google" specifically - it all goes through the
`OAuthProviderClient` interface, so adding GitHub etc. later is additive.
"""
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from app.core.exceptions import InvalidCredentialsError, InvalidTokenError, OAuthAccountLinkingRequiredError
from app.core.security import verify_password
from app.modules.auth.repositories.auth_provider_repository import AuthProviderRepository
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.auth.schemas import TokenResponse
from app.modules.auth.oauth.base import OAuthProviderClient, OAuthUserInfo
from app.modules.auth.token_service import TokenService

_LINK_TOKEN_TYPE = "oauth_link"
_LINK_TOKEN_EXPIRE_MINUTES = 10


class OAuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        auth_provider_repo: AuthProviderRepository,
        token_service: TokenService,
    ):
        self.user_repo = user_repo
        self.auth_provider_repo = auth_provider_repo
        self.token_service = token_service

    async def authenticate(self, client: OAuthProviderClient, code: str) -> TokenResponse:
        """
        Completes the OAuth flow for `code`. Raises
        `OAuthAccountLinkingRequiredError` (carrying a `link_token`) instead
        of returning tokens when the email isn't verified by the provider but
        matches an existing password account.
        """
        info = await client.fetch_user_info(code)

        existing_link = await self.auth_provider_repo.get_by_provider_identity(
            info.provider, info.provider_user_id
        )
        if existing_link is not None:
            return await self.token_service.issue_token_pair(existing_link.user_id)

        existing_user = await self.user_repo.get_by_email(info.email)

        if existing_user is None:
            # Prefer Google's display name; fall back to the email local-part.
            display_name = (info.name or "").strip() or info.email.split("@", 1)[0]
            new_user = await self.user_repo.create(
                email=info.email, hashed_password=None, name=display_name
            )
            await self.auth_provider_repo.create(new_user.id, info.provider, info.provider_user_id)
            return await self.token_service.issue_token_pair(new_user.id)

        if info.email_verified:
            await self.auth_provider_repo.create(existing_user.id, info.provider, info.provider_user_id)
            return await self.token_service.issue_token_pair(existing_user.id)

        # Email exists but Google didn't vouch for it - could be a spoofed/unverified
        # address on Google's side. Require proof of ownership via the existing password.
        link_token = self._create_link_token(info)
        raise OAuthAccountLinkingRequiredError(link_token=link_token)

    async def confirm_link(self, link_token: str, password: str) -> TokenResponse:
        """Completes linking after the user proves ownership of the existing
        password account (called from a dedicated confirm-link endpoint)."""
        try:
            payload = jwt.decode(
                link_token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
            )
        except jwt.PyJWTError as exc:
            raise InvalidTokenError("Invalid or expired link token") from exc

        if payload.get("type") != _LINK_TOKEN_TYPE:
            raise InvalidTokenError("Invalid link token")

        user = await self.user_repo.get_by_email(payload["email"])
        if user is None or user.hashed_password is None:
            raise InvalidCredentialsError()

        if not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError()

        await self.auth_provider_repo.create(
            user.id, payload["provider"], payload["provider_user_id"]
        )
        return await self.token_service.issue_token_pair(user.id)

    @staticmethod
    def _create_link_token(info: OAuthUserInfo) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "type": _LINK_TOKEN_TYPE,
            "provider": info.provider,
            "provider_user_id": info.provider_user_id,
            "email": info.email,
            "iat": now,
            "exp": now + timedelta(minutes=_LINK_TOKEN_EXPIRE_MINUTES),
        }
        return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
