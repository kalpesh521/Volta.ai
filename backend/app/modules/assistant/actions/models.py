"""Audit row for a proposed device action. The LangGraph checkpoint holds the pause."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import GUID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActionProposal(Base):
    __tablename__ = "action_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    device: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=func.now(), nullable=False
    )
