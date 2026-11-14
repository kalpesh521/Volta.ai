"""
Stored automation rules (tracker 4.1, 4.2).

One row per rule a user has asked for. The rule itself is the validated DSL as JSON, with its fingerprint: approving a
rule approves *that* fingerprint, and the executor refuses to run a row whose JSON no longer hashes to it, so editing a
row (or a bug) cannot silently change what the user agreed to.

status: pending_approval -> active <-> paused ; pending_approval -> rejected
Only `active` rules ever run. A rule written by the assistant starts as `pending_approval` and does nothing until the
user approves it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import GUID


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationRuleDef(Base):
    __tablename__ = "automation_rule_defs"
    __table_args__ = (
        UniqueConstraint("household_id", "rule_id", name="uq_automation_rule_per_home"),
        Index("ix_automation_rule_defs_active", "household_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    household_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)  # the DSL's own id, unique per home
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_json: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_approval", server_default="pending_approval")
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="api", server_default="api")  # agent | api
    dry_run_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    state_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    pause_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=_utcnow, nullable=False
    )
