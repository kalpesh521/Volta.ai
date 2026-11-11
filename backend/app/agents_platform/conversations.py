"""
Conversation registry: who owns which chat thread, and when it was last used.

The conversation *content* lives in the LangGraph checkpoint (thread memory). This table exists because
a checkpoint has no owner and no queryable age, which a production system needs for two things:

* **Ownership.** The server issues conversation ids. A client cannot invent one or resume somebody
  else's; an unknown or foreign id looks identical (404), so ids cannot be probed.
* **Retention.** A sweep deletes conversations idle longer than the retention period, together with
  their checkpoints. Without it the checkpoint tables grow forever.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.exceptions import AppError
from app.core.types import GUID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False, index=True
    )


class ConversationNotFoundError(AppError):
    status_code = 404
    code = "conversation_not_found"

    def __init__(self) -> None:
        super().__init__("Conversation not found.")


@dataclass(frozen=True)
class ConversationRef:
    id: str
    user_id: uuid.UUID
    household_id: str

    @property
    def thread_id(self) -> str:
        """The LangGraph thread. Scoped by user and home as well, so an id alone can never address a thread."""
        return f"chat:{self.user_id}:{self.household_id}:{self.id}"


def thread_id_for(row: AssistantConversation) -> str:
    return ConversationRef(row.id, row.user_id, row.household_id).thread_id


class ConversationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def open(
        self, conversation_id: str | None, *, user_id: uuid.UUID, household_id: str
    ) -> ConversationRef:
        """Start a conversation, or resume one this user owns for this home."""
        if conversation_id is None:
            created = AssistantConversation(id=uuid.uuid4().hex, user_id=user_id, household_id=household_id)
            self._db.add(created)
            await self._db.flush()
            return ConversationRef(created.id, user_id, household_id)

        row = (
            await self._db.execute(
                select(AssistantConversation).where(
                    AssistantConversation.id == conversation_id,
                    AssistantConversation.user_id == user_id,
                    AssistantConversation.household_id == household_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConversationNotFoundError()
        return ConversationRef(row.id, row.user_id, row.household_id)

    async def touch(self, conversation_id: str, *, now: datetime | None = None) -> None:
        await self._db.execute(
            update(AssistantConversation)
            .where(AssistantConversation.id == conversation_id)
            .values(last_active_at=now or _utcnow())
            .execution_options(synchronize_session=False)
        )

    async def idle_since(self, cutoff: datetime, *, limit: int = 500) -> list[AssistantConversation]:
        result = await self._db.execute(
            select(AssistantConversation)
            .where(AssistantConversation.last_active_at < cutoff)
            .order_by(AssistantConversation.last_active_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def delete(self, rows: list[AssistantConversation]) -> None:
        for row in rows:
            await self._db.delete(row)
        await self._db.flush()
