"""
Issues and rotates access/refresh token pairs. Centralized here so both the
password-login flow and the OAuth flow produce identical token shapes -
the frontend never needs to know which auth method the user used.
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import InvalidTokenError
from app.core.security import create_access_token, generate_opaque_token, hash_token
from app.modules.auth.repositories.refresh_token_repository import RefreshTokenRepository
from app.modules.auth.schemas import TokenResponse


class TokenService:
    def __init__(self, refresh_token_repo: RefreshTokenRepository):
        self.refresh_token_repo = refresh_token_repo

    async def issue_token_pair(self, user_id: uuid.UUID) -> TokenResponse:
        """Creates a brand-new access + refresh token pair (used on login/OAuth)."""
        access_token = create_access_token(user_id)
        raw_refresh_token = generate_opaque_token()
        expires_at = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

        await self.refresh_token_repo.create(
            user_id=user_id,
            token_hash=hash_token(raw_refresh_token),
            expires_at=expires_at,
        )

        return TokenResponse(access_token=access_token, refresh_token=raw_refresh_token)

    async def rotate_refresh_token(self, raw_refresh_token: str) -> TokenResponse:
        """
        Validates the given refresh token, revokes it, and issues a brand-new
        access + refresh token pair (rotation). If a token is reused after
        being rotated/revoked, we treat it as a potential theft signal and
        revoke every active session for that user.
        """
        token_hash = hash_token(raw_refresh_token)
        stored_token = await self.refresh_token_repo.get_by_hash(token_hash)

        if stored_token is None:
            raise InvalidTokenError("Refresh token is invalid")

        if not self.refresh_token_repo.is_valid(stored_token):
            if stored_token.revoked_at is not None:
                # Reuse of a rotated/revoked token - assume compromise, nuke all sessions.
                await self.refresh_token_repo.revoke_all_for_user(stored_token.user_id)
            raise InvalidTokenError("Refresh token is invalid or expired")

        await self.refresh_token_repo.revoke(stored_token)
        return await self.issue_token_pair(stored_token.user_id)

    async def revoke_refresh_token(self, raw_refresh_token: str) -> None:
        """Used by logout - idempotent, doesn't error if the token is already gone."""
        token_hash = hash_token(raw_refresh_token)
        stored_token = await self.refresh_token_repo.get_by_hash(token_hash)
        if stored_token is not None and stored_token.revoked_at is None:
            await self.refresh_token_repo.revoke(stored_token)
