"""Audit rows for proposed device actions and the overrides they create. The LangGraph checkpoint holds the pause."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
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
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set while the proposal waits for the user. After this time it can no longer be confirmed.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=func.now(), nullable=False
    )


class ActionAuditEvent(Base):
    __tablename__ = "action_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )


class DeviceOverride(Base):
    __tablename__ = "device_overrides"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    device: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(String(16), nullable=False)
    power_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )


class DeviceCommandOutbox(Base):
    """
    A device command accepted for delivery (transactional outbox, tracker 4.3 and 4.5).

    Confirming an action writes this row in the same database transaction as the proposal's new status and its audit
    event, so "queued" always means a durable record exists, and a command can never reach the broker for a request that
    then rolled back. A relay publishes pending rows after the commit. `command_id` is unique, so the same command is
    never recorded twice, and it is the message id the consumer de-duplicates on.

    status: pending -> sent | blocked (kill switch) | expired (too old to be safe) | dead (retries used up)
    """

    __tablename__ = "device_command_outbox"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    command_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_device_command_outbox_due", "status", "next_attempt_at"),)
