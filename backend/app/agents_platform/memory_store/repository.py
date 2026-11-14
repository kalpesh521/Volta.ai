"""
Scoped queries for `user_memory_items`.

Every method takes the user id AND the household id and puts both in the WHERE clause. There is no query by item
id alone, so a foreign or unknown id is simply "not found": ids cannot be probed across users or across homes.
Expired items are invisible to every read; `delete_expired` removes them for good.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.memory_store.models import UserMemoryItem, utcnow


def _live(now: datetime):
    return or_(UserMemoryItem.expires_at.is_(None), UserMemoryItem.expires_at > now)


def _scope(user_id: uuid.UUID, household_id: str):
    return and_(UserMemoryItem.user_id == user_id, UserMemoryItem.household_id == household_id)


class MemoryRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(
        self, user_id: uuid.UUID, household_id: str, item_id: str, *, now: datetime | None = None
    ) -> UserMemoryItem | None:
        result = await self._db.execute(
            select(UserMemoryItem).where(
                _scope(user_id, household_id), UserMemoryItem.id == item_id, _live(now or utcnow())
            )
        )
        return result.scalar_one_or_none()

    async def list_active(
        self, user_id: uuid.UUID, household_id: str, *, now: datetime | None = None, limit: int | None = None
    ) -> list[UserMemoryItem]:
        """Newest first (last change wins), id as the tie-break so the order is stable."""
        query = (
            select(UserMemoryItem)
            .where(_scope(user_id, household_id), _live(now or utcnow()))
            .order_by(UserMemoryItem.updated_at.desc(), UserMemoryItem.id)
        )
        if limit is not None:
            query = query.limit(limit)
        return list((await self._db.execute(query)).scalars())

    async def count_active(self, user_id: uuid.UUID, household_id: str, *, now: datetime | None = None) -> int:
        result = await self._db.execute(
            select(func.count()).select_from(UserMemoryItem).where(
                _scope(user_id, household_id), _live(now or utcnow())
            )
        )
        return int(result.scalar_one())

    async def find_by_hash(
        self, user_id: uuid.UUID, household_id: str, text_hash: str
    ) -> UserMemoryItem | None:
        """Includes expired rows (they still hold the unique key until the sweep removes them)."""
        result = await self._db.execute(
            select(UserMemoryItem).where(_scope(user_id, household_id), UserMemoryItem.text_hash == text_hash)
        )
        return result.scalar_one_or_none()

    async def add(self, item: UserMemoryItem) -> UserMemoryItem:
        self._db.add(item)
        await self._db.flush()
        return item

    async def flush(self) -> None:
        await self._db.flush()

    async def delete(self, item: UserMemoryItem) -> None:
        """Hard delete: the row is gone, not flagged."""
        await self._db.delete(item)
        await self._db.flush()

    async def delete_all(self, user_id: uuid.UUID, household_id: str) -> int:
        """Every item of this user in this home, expired ones included."""
        result = await self._db.execute(
            delete(UserMemoryItem)
            .where(_scope(user_id, household_id))
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def delete_all_homes(self, user_id: uuid.UUID) -> int:
        """Erasure of one user's whole memory (account deletion, DPDP)."""
        result = await self._db.execute(
            delete(UserMemoryItem).where(UserMemoryItem.user_id == user_id).execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def delete_expired(self, now: datetime) -> int:
        result = await self._db.execute(
            delete(UserMemoryItem)
            .where(UserMemoryItem.expires_at.is_not(None), UserMemoryItem.expires_at <= now)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    async def mark_used(
        self, user_id: uuid.UUID, household_id: str, ids: Sequence[str], *, now: datetime
    ) -> int:
        if not ids:
            return 0
        result = await self._db.execute(
            update(UserMemoryItem)
            .where(_scope(user_id, household_id), UserMemoryItem.id.in_(list(ids)))
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)
