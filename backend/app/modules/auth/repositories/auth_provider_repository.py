"""DB access for linked OAuth identities (auth_providers table)."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth_provider import AuthProvider


class AuthProviderRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_provider_identity(
        self, provider: str, provider_user_id: str
    ) -> AuthProvider | None:
        result = await self.db.execute(
            select(AuthProvider).where(
                AuthProvider.provider == provider,
                AuthProvider.provider_user_id == provider_user_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self, user_id: uuid.UUID, provider: str, provider_user_id: str
    ) -> AuthProvider:
        link = AuthProvider(user_id=user_id, provider=provider, provider_user_id=provider_user_id)
        self.db.add(link)
        await self.db.flush()
        return link
