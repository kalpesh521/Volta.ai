"""HTTP contract for the v2 assistant. A superset of the v1 response, so a client can switch endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents_platform.contracts import AgentName, AgentStatus
from app.modules.assistant.actions.schemas import ActionResponse
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


class SupervisorResponse(AssistantResponse):
    conversation_id: str
    route: str = Field(description="single, multi, exclusive or refusal")
    agents: list[AgentRunOut]
    proposal: ActionResponse | None = Field(
        default=None,
        description="A device action waiting for confirmation. Confirm it with /assistant/actions/{id}/confirm.",
    )
