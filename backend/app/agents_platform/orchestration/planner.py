"""
Intents → plan: which specialist agents answer this question.

Deterministic on purpose, like the v1 tool planner: routing is auditable, costs no tokens and
is covered by the trajectory evals. The rules:

* `device_control` is an exclusive route. It never runs beside another agent and drops the
  other intents (v1 behaves the same).
* `out_of_scope` runs no agent at all.
* `document` goes to the Knowledge agent, every other intent to Energy Insight.
* Intents that need both agents run them in parallel and the synthesizer merges the facts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agents_platform.contracts import MAX_AGENTS_PER_PLAN, AgentCall, AgentName, Plan
from app.modules.assistant.domain.answers import FallbackReason
from app.modules.assistant.domain.intents import Intent

_KNOWLEDGE_INTENTS = frozenset({Intent.DOCUMENT})


def _agent_for(intent: Intent) -> AgentName:
    return AgentName.KNOWLEDGE if intent in _KNOWLEDGE_INTENTS else AgentName.ENERGY_INSIGHT


def build_agent_plan(
    *,
    question: str,
    intents: Sequence[Intent],
    appliance: str | None,
    deadlines: Mapping[AgentName, float],
) -> Plan:
    """Build the plan for one classified question. `intents[0]` is the primary intent."""
    if not intents:
        intents = (Intent.OUT_OF_SCOPE,)
    primary = intents[0]

    if primary is Intent.DEVICE_CONTROL:
        call = AgentCall(
            agent=AgentName.DEVICE_ACTION,
            question=question,
            intents=(Intent.DEVICE_CONTROL.value,),
            deadline_seconds=deadlines[AgentName.DEVICE_ACTION],
        )
        return Plan(calls=(call,), exclusive=True)

    if primary is Intent.OUT_OF_SCOPE:
        return Plan(refusal=FallbackReason.OUT_OF_SCOPE.value)

    by_agent: dict[AgentName, list[Intent]] = {}
    for intent in intents:
        if intent in (Intent.DEVICE_CONTROL, Intent.OUT_OF_SCOPE):
            continue  # a secondary control or off-topic intent never adds an agent
        by_agent.setdefault(_agent_for(intent), []).append(intent)

    # The primary intent's agent is listed first, so it is the one whose `primary` rules apply
    # when results are merged.
    ordered = sorted(by_agent, key=lambda agent: agent is not _agent_for(primary))
    calls = tuple(
        AgentCall(
            agent=agent,
            question=question,
            intents=tuple(intent.value for intent in by_agent[agent]),
            appliance=appliance if agent is AgentName.ENERGY_INSIGHT else None,
            deadline_seconds=deadlines[agent],
        )
        for agent in ordered[:MAX_AGENTS_PER_PLAN]
    )
    return Plan(calls=calls)
