"""The `user_memory_items` table."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import GUID


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryKind(StrEnum):
    """Closed set. The kind helps the user scan the page and lets a later agent pick relevant items."""

    PREFERENCE = "preference"
    APPLIANCE_HABIT = "appliance_habit"
    GOAL = "goal"
    EQUIPMENT = "equipment"
    OTHER = "other"


class MemorySource(StrEnum):
    COMMAND = "command"  # "remember that ..." typed in the chat
    API = "api"  # the "What Suryaa remembers" page or another client


class UserMemoryItem(Base):
    __tablename__ = "user_memory_items"
    __table_args__ = (
        # Dedupe by normalised text, per user and home. Also closes the race between two parallel saves.
        UniqueConstraint("user_id", "household_id", "text_hash", name="uq_user_memory_text"),
        Index("ix_user_memory_scope_updated", "user_id", "household_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default=MemoryKind.OTHER.value)
    # Masked text: personal data was replaced by tokens before it got here. Never the raw input.
    text: Mapped[str] = mapped_column(String(400), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
