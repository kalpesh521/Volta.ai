"""HTTP contract for propose → confirm. Nothing is applied inside this response."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProposeActionRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class ConfirmActionRequest(BaseModel):
    confirmed: bool


class AuditEventOut(BaseModel):
    event: str
    command_id: str | None = None
    created_at: datetime


class ActionResponse(BaseModel):
    proposal_id: str
    status: Literal["rejected", "awaiting_confirmation", "expired", "cancelled", "queued", "applied", "failed"]
    device: str | None = None
    command: str | None = None
    duration_minutes: int | None = None
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    message: str
    command_id: str | None = None
    expires_at: datetime | None = None
    audit: list[AuditEventOut] = Field(default_factory=list)
