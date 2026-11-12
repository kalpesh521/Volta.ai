"""
Gate G5: which agent may call which tool (doc 04 §1 and §5).

Today the tools are bound to one household by a closure, so no agent can reach another tenant's data. This
module adds the second half: an agent can only reach the tools that belong to its job. The Knowledge agent
cannot read live telemetry, and the Device Action agent has no data tools at all (it proposes through its own
workflow). Plain Python and plain data, tested in CI like code. Adopt OPA only if these rules ever need to
change without a deploy (doc 11).

The action × device rules of G6 (critical devices, battery reserve, inverter capacity, duplicates, kill
switch) already live in `device_action/safety.py` and are unchanged.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import TypeVar

from app.agents_platform.contracts import AgentName
from app.core.metrics import POLICY_DENIALS
from app.modules.assistant.tools.registry import ToolName

logger = logging.getLogger("volta.security")

T = TypeVar("T")

AGENT_TOOLS: Mapping[AgentName, frozenset[ToolName]] = {
    # Energy Insight reads the whole home. A full overview also looks up documents, as v1 always did.
    AgentName.ENERGY_INSIGHT: frozenset(ToolName),
    AgentName.KNOWLEDGE: frozenset({ToolName.SEARCH_KNOWLEDGE, ToolName.HOUSEHOLD_PROFILE}),
    AgentName.DEVICE_ACTION: frozenset(),
}


class ToolForbiddenError(PermissionError):
    def __init__(self, agent: AgentName, tool: ToolName) -> None:
        super().__init__(f"agent {agent.value} may not call {tool.value}")
        self.agent = agent
        self.tool = tool


def is_allowed(agent: AgentName, tool: ToolName) -> bool:
    return tool in AGENT_TOOLS.get(agent, frozenset())


class AuthorizedTools(Mapping[ToolName, T]):
    """
    The tool map an agent is handed. A tool outside the agent's allow-list is not in it, and asking for one is
    refused loudly: counted, logged as a security event, and raised as `ToolForbiddenError` (the tool node turns
    that into an "error" result like any other tool failure).
    """

    def __init__(self, agent: AgentName, tools: Mapping[ToolName, T]) -> None:
        self._agent = agent
        self._tools = {name: tool for name, tool in tools.items() if is_allowed(agent, name)}

    def __getitem__(self, name: ToolName) -> T:
        try:
            return self._tools[name]
        except KeyError:
            if isinstance(name, ToolName) and not is_allowed(self._agent, name):
                POLICY_DENIALS.labels(agent=self._agent.value, tool=name.value).inc()
                logger.warning("tool denied agent=%s tool=%s", self._agent.value, name.value)
                raise ToolForbiddenError(self._agent, name) from None
            raise

    def __contains__(self, name: object) -> bool:
        # Asking whether a tool is there is not a call to it: no denial is raised, counted or logged.
        return name in self._tools

    def __iter__(self) -> Iterator[ToolName]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)
