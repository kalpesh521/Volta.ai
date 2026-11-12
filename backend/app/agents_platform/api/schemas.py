"""HTTP contract for the v2 assistant. A superset of the v1 response, so a client can switch endpoints."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agents_platform.contracts import AgentName, AgentStatus
from app.agents_platform.agents.device_action.schemas import ActionResponse
from app.modules.assistant.schemas import AskRequest, AssistantResponse

CONVERSATION_ID_PATTERN = r"^[a-f0-9]{32}$"


class AskV2Request(AskRequest):
    conversation_id: str | None = Field(
        default=None,
        pattern=CONVERSATION_ID_PATTERN,
        description="Id from an earlier response, to continue that conversation. Omit to start a new one.",
    )


class AgentRunOut(BaseModel):
    agent: AgentName
    status: AgentStatus
    latency_ms: int
    confidence: str


class GuardOut(BaseModel):
    """What the input guard decided about the question. Never contains the question's raw personal data."""

    decision: Literal["allow", "review", "block"]
    reason: str | None = Field(default=None, description="Why a question was refused, when it was.")
    categories: list[str] = Field(default_factory=list, description="Injection signals that fired.")
    pii_masked: dict[str, int] = Field(
        default_factory=dict, description="How many values of each personal-data class were masked."
    )
    untrusted: bool = Field(default=False, description="Treated cautiously: no router model, no device action.")


class BudgetOut(BaseModel):
    tier: str
    daily_limit: int
    remaining: int = Field(description="Tokens left today, after this request.")
    resets_in_seconds: int
    degraded: bool = Field(default=False, description="True when this answer was written without the model.")


class SupervisorResponse(AssistantResponse):
    conversation_id: str
    route: str = Field(description="single, multi, exclusive or refusal")
    agents: list[AgentRunOut]
    guard: GuardOut | None = None
    budget: BudgetOut | None = None
    proposal: ActionResponse | None = Field(
        default=None,
        description="A device action waiting for confirmation. Confirm it with /assistant/actions/{id}/confirm.",
    )
