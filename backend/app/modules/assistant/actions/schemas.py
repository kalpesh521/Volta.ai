"""HTTP contract for propose → confirm. Nothing is applied inside this response."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProposeActionRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)


class ConfirmActionRequest(BaseModel):
    confirmed: bool


class ActionResponse(BaseModel):
    proposal_id: str
    status: Literal["rejected", "awaiting_confirmation", "cancelled", "queued", "failed"]
    device: str | None = None
    command: str | None = None
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    message: str
    command_id: str | None = None
