"""HTTP contracts for automation rules."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["pending_approval", "active", "paused", "rejected"]


class AutomationOut(BaseModel):
    id: uuid.UUID
    rule_id: str
    name: str
    status: Status
    source: Literal["agent", "api"]
    summary: str = Field(description="What the rule does, in plain words, generated from the rule itself.")
    rule: dict[str, Any]
    fingerprint: str
    dry_run: dict[str, Any] = Field(default_factory=dict, description="What the rule would have done on recent readings.")
    pause_reason: str | None = None
    approved_at: datetime | None = None
    last_fired_at: datetime | None = None
    fire_count: int = 0
    created_at: datetime


class ProposeAutomationRequest(BaseModel):
    rule: dict[str, Any] = Field(description="A rule in the automation DSL. It is validated; it starts pending approval.")


class ApproveAutomationRequest(BaseModel):
    fingerprint: str = Field(
        min_length=64, max_length=64, description="The fingerprint shown on the approval card: approving binds to exactly that rule."
    )
