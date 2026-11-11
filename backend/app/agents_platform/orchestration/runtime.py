"""
Per-request dependencies for the supervisor graph.

The graph is compiled once per process. Everything that belongs to one request (the household,
the user, the database session behind the action service) arrives here through `context=`, never
through module globals and never through the LLM.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from langchain_core.runnables import RunnableConfig

from app.agents_platform.contracts import AgentCall, AgentName, AgentResult
from app.ai.config import AISettings
from app.ai.llm.gateway import StructuredLLM


class SpecialistAgent(Protocol):
    """One specialist. It returns a typed result; the supervisor owns deadlines and failures."""

    name: AgentName

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult: ...


@dataclass(frozen=True)
class SupervisorRuntime:
    agents: Mapping[AgentName, SpecialistAgent]
    llm: StructuredLLM
    settings: AISettings

    def deadlines(self) -> dict[AgentName, float]:
        return {
            AgentName.ENERGY_INSIGHT: self.settings.AI_ENERGY_AGENT_DEADLINE_SECONDS,
            AgentName.KNOWLEDGE: self.settings.AI_KNOWLEDGE_AGENT_DEADLINE_SECONDS,
            AgentName.DEVICE_ACTION: self.settings.AI_DEVICE_ACTION_AGENT_DEADLINE_SECONDS,
        }
