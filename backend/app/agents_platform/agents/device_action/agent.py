"""
Device Action agent (L2: acts only after the user confirms). An exclusive route.

The agent never sends a command. It asks the existing action workflow to *propose* one: parse the
request, load live facts, run the deterministic safety checks, and pause on a durable `interrupt`.
The user then confirms or rejects through the action endpoints, which resume that paused graph,
re-check safety and publish. That lifecycle (2-minute deadline, single-use claim, replay guard,
audit trail) is the Phase 0 one and is unchanged.

No LLM is involved: the wording is deterministic, built from the proposal the safety layer produced.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Protocol

from langchain_core.runnables import RunnableConfig

from app.agents_platform.contracts import AgentCall, AgentName, AgentResult, AgentStatus, AnswerParts
from app.agents_platform.agents.device_action.schemas import ActionResponse
from app.modules.onboarding.models import SolarSystem

# Marks a result whose deterministic draft is the answer and must not be rewritten by an LLM.
DEVICE_ACTION_REASON = "device_action"


class ActionProposer(Protocol):
    """The slice of `ActionService` this agent needs. A port, so the agent is testable without a database."""

    async def propose(
        self, system: SolarSystem, user_id: uuid.UUID, question: str
    ) -> ActionResponse: ...


class DeviceActionAgent:
    name = AgentName.DEVICE_ACTION

    def __init__(self, proposer: ActionProposer, *, system: SolarSystem, user_id: uuid.UUID) -> None:
        self._proposer = proposer
        self._system = system
        self._user_id = user_id

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult:
        started = time.perf_counter()
        proposal = await self._proposer.propose(self._system, self._user_id, call.question)
        awaiting = proposal.status == "awaiting_confirmation"
        return AgentResult(
            agent=self.name,
            status=AgentStatus.OK if awaiting else AgentStatus.REFUSED,
            draft=_wording(proposal),
            fallback_reason=DEVICE_ACTION_REASON,
            proposals=[proposal.model_dump(mode="json")],
            confidence="high",
            live_advice=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _wording(proposal: ActionResponse) -> AnswerParts:
    if proposal.status == "awaiting_confirmation":
        return AnswerParts(
            observation=proposal.message,
            explanation="The safety checks passed. Nothing has changed on the device yet.",
            recommendation=_confirm_hint(proposal.expires_at),
            estimated_impact="None until you confirm.",
        )
    return AnswerParts(
        observation=proposal.message,
        explanation="The request did not go through, so no command was sent.",
        recommendation="Use the appliance's own switch, or ask again when the conditions change.",
        estimated_impact="None. No device was changed.",
    )


def _confirm_hint(expires_at: datetime | None) -> str:
    if expires_at is None:
        return "Confirm or reject this request in the app."
    stamp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
    seconds = max(0, int((stamp - datetime.now(timezone.utc)).total_seconds()))
    return f"Confirm or reject this request in the app within {seconds} seconds."
