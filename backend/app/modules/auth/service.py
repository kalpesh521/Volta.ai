"""Business logic for signup, login, and password reset (email/password flows)."""
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import EmailAlreadyRegisteredError, InvalidCredentialsError, InvalidTokenError
from app.core.security import generate_opaque_token, hash_password, hash_token, verify_password
from app.models.user import User
from app.modules.auth.repositories.password_reset_repository import PasswordResetRepository
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.auth.schemas import TokenResponse
from app.modules.auth.user_schemas import UserCreate
from app.modules.auth.token_service import TokenService


class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        token_service: TokenService,
        password_reset_repo: PasswordResetRepository,
    ):
        self.user_repo = user_repo
        self.token_service = token_service
        self.password_reset_repo = password_reset_repo

    async def signup(self, data: UserCreate) -> User:
        existing = await self.user_repo.get_by_email(data.email)
        if existing is not None:
            raise EmailAlreadyRegisteredError()

        hashed = hash_password(data.password)
        return await self.user_repo.create(
            email=data.email, hashed_password=hashed, name=data.name.strip()
        )

    async def login(self, email: str, password: str) -> TokenResponse:
        user = await self.user_repo.get_by_email(email)

        # Same generic error whether the user doesn't exist, has no password
        # (OAuth-only account), or the password is wrong - never leak which case it is.
        if user is None or user.hashed_password is None:
            raise InvalidCredentialsError()

        if not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise InvalidCredentialsError()

        return await self.token_service.issue_token_pair(user.id)

    async def request_password_reset(self, email: str) -> None:
        """
        Always returns successfully regardless of whether the email exists,
        so this endpoint can't be used to enumerate registered accounts.
        The raw token would normally be emailed to the user; here we log it
        (see note in the router) since sending real email is out of scope.
        """
        user = await self.user_repo.get_by_email(email)
        if user is None:
            return

        raw_token = generate_opaque_token()
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
        )
        await self.password_reset_repo.create(
            user_id=user.id, token_hash=hash_token(raw_token), expires_at=expires_at
        )

        # STUB: in production this would enqueue a "reset your password" email
        # containing a link with `raw_token`. We simulate that by logging it.
        print(f"[password-reset] simulated email to {email}: reset token = {raw_token}")

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        token_hash = hash_token(raw_token)
        stored_token = await self.password_reset_repo.get_by_hash(token_hash)

        if stored_token is None or not self.password_reset_repo.is_valid(stored_token):
            raise InvalidTokenError("Password reset token is invalid or expired")

        user = await self.user_repo.get_by_id(stored_token.user_id)
        if user is None:
            raise InvalidTokenError("Password reset token is invalid or expired")

        await self.user_repo.set_password(user, hash_password(new_password))
        await self.password_reset_repo.mark_used(stored_token)
