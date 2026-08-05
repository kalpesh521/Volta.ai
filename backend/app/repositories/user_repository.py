"""
Pure DB-access layer for users. No business rules here (e.g. no password
verification, no "does this email already exist -> raise" logic) - that
belongs in `services/`. Repositories just read/write rows.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.db.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(
        self, email: str, hashed_password: str | None, name: str
    ) -> User:
        user = User(email=email, hashed_password=hashed_password, name=name)
        self.db.add(user)
        await self.db.flush()  # populates user.id without ending the transaction
        return user

    async def set_password(self, user: User, hashed_password: str) -> User:
        user.hashed_password = hashed_password
        await self.db.flush()
        return user
