"""
The contract between the supervisor and its specialist agents.

Agents never talk to each other and never answer the user directly. The supervisor sends an
`AgentCall` and gets an `AgentResult` back. Everything here is a plain pydantic model that
dumps to JSON, because results travel through graph state and therefore through checkpoints.

An agent is a failure boundary: whatever goes wrong inside it comes back as a typed result with
a `status`, never as an exception that would take its siblings down.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.assistant.graph.state import LLMCallRecord, ToolCallRecord

Confidence = Literal["high", "medium", "low"]


class AgentName(StrEnum):
    """Closed set. The graph registers one node per member, so a typo cannot dispatch to nowhere."""

    ENERGY_INSIGHT = "energy_insight"
    KNOWLEDGE = "knowledge"
    DEVICE_ACTION = "device_action"
    # Planning agents (Phase 3). They compute with deterministic engines and never write the answer themselves.
    FORECAST = "forecast"
    OPTIMIZER = "optimizer"
    BILL_TARIFF = "bill_tariff"


# The planner enforces that `DEVICE_ACTION` is exclusive: it can change the world, so it never
# shares a plan with another agent. The others may run side by side.
MAX_AGENTS_PER_PLAN = 4


class AgentStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"  # useful result, but something it needed was missing
    REFUSED = "refused"  # the request is outside what this agent may do
    ERROR = "error"
    TIMEOUT = "timeout"
    DISABLED = "disabled"  # switched off by a feature flag: not run, not an error

    @property
    def usable(self) -> bool:
        return self in (AgentStatus.OK, AgentStatus.PARTIAL, AgentStatus.REFUSED)


class AgentCall(BaseModel):
    """What the supervisor asks one agent to do. Carries only what that agent needs."""

    model_config = ConfigDict(frozen=True)

    agent: AgentName
    question: str
    intents: tuple[str, ...] = ()
    appliance: str | None = None
    deadline_seconds: float = Field(gt=0)


class AnswerParts(BaseModel):
    """The four wording fields. The data time is stamped later by the supervisor."""

    observation: str
    explanation: str
    recommendation: str
    estimated_impact: str


class AgentResult(BaseModel):
    """
    What one agent returns.

    `facts`, `analytics` and `freshness` are the same projected payloads the answer prompt uses,
    so the output gate can check an answer's numbers against exactly what the model was shown.
    `draft` is deterministic wording the agent can stand behind without an LLM (a refusal, a
    knowledge gap, a device-action explanation). It is also the fallback when the LLM is skipped.
    """

    agent: AgentName
    status: AgentStatus
    facts: dict[str, Any] = Field(default_factory=dict)
    analytics: dict[str, Any] = Field(default_factory=dict)
    freshness: dict[str, Any] = Field(default_factory=dict)
    analytics_used: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)  # proposed, never executed here
    draft: AnswerParts | None = None
    fallback_reason: str | None = None  # set when the answer must not come from the LLM
    warnings: list[str] = Field(default_factory=list)
    confidence: Confidence = "low"
    live_advice: bool = True
    requires_telemetry: bool = False
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    llm_calls: list[LLMCallRecord] = Field(default_factory=list)
    latency_ms: int = 0

    @classmethod
    def failed(cls, agent: AgentName, status: AgentStatus, *, latency_ms: int = 0) -> AgentResult:
        """A result for an agent that timed out or crashed. Has no facts and no draft."""
        return cls(agent=agent, status=status, latency_ms=latency_ms, confidence="low")

    @property
    def answers_itself(self) -> bool:
        """True when its deterministic draft is the answer and no LLM should rewrite it."""
        return self.draft is not None and self.fallback_reason is not None


class Plan(BaseModel):
    """Which agents run for one question. An empty `agents` list with a `refusal` means no agent runs."""

    model_config = ConfigDict(frozen=True)

    calls: tuple[AgentCall, ...] = ()
    exclusive: bool = False
    refusal: str | None = None  # a FallbackReason value

    @property
    def route(self) -> Literal["single", "multi", "exclusive", "refusal"]:
        if self.refusal:
            return "refusal"
        if self.exclusive:
            return "exclusive"
        return "single" if len(self.calls) == 1 else "multi"
