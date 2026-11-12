"""
Output checks that need no facts (claims, unsafe advice) and the tool × agent policy.

The policy test that matters most is the invariant at the bottom: every tool the planner can select for an agent
must be one that agent is allowed to call. If someone adds a tool to an intent plan and forgets the policy, an
agent would fail at runtime; this makes it fail in CI instead.
"""
from __future__ import annotations

import pytest

from app.agents_platform.contracts import AgentName
from app.agents_platform.guardrails.output_guard import Kind, check_answer
from app.agents_platform.guardrails.policy import (
    AGENT_TOOLS,
    AuthorizedTools,
    ToolForbiddenError,
    is_allowed,
)
from app.agents_platform.orchestration.planner import build_agent_plan
from app.core.metrics import POLICY_DENIALS
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.graph.planner import build_plan
from app.modules.assistant.tools.registry import ToolName


def answer(**fields: str) -> dict[str, str]:
    base = {"observation": "", "explanation": "", "recommendation": "", "estimated_impact": "", "data_time": "now"}
    return {**base, **fields}


# --- output claims --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,rule",
    [
        ("I have turned off the geyser.", "first_person_past"),
        ("I've switched on the AC for you.", "first_person_past"),
        ("We shut down the pump.", "first_person_past"),
        ("I scheduled the washing machine for noon.", "first_person_past"),
        ("I will turn on the heater at 2 pm.", "first_person_future"),
        ("I'll switch the inverter to grid.", "first_person_future"),
        ("The geyser has been turned off.", "passive_completed"),
        ("Your AC has been switched on.", "passive_completed"),
    ],
)
def test_a_claim_of_having_acted_on_a_device_is_flagged(text, rule):
    violations = check_answer(answer(observation=text))
    assert [(v.kind, v.rule) for v in violations] == [(Kind.ACTION_CLAIM, rule)]


@pytest.mark.parametrize(
    "text",
    [
        "Open the inverter cover and check the terminals.",
        "You can bypass the breaker to restore power.",
        "Disable the BMS to force a charge.",
        "Tamper with the meter to lower the reading.",
    ],
)
def test_dangerous_electrical_advice_is_flagged(text):
    assert {v.kind for v in check_answer(answer(recommendation=text))} == {Kind.UNSAFE_ADVICE}


@pytest.mark.parametrize(
    "text",
    [
        "Solar covers the load and the battery is at 62%.",
        "You can turn off the geyser at its switch to save 1.2 kWh.",  # advice to the user, not a claim
        "I started by looking at your battery data.",  # "started" is not a state change
        "The inverter has been switched to grid mode by its schedule.",  # a fact about the data
        "The battery switched to discharging at 7 pm.",
        "Consider running the washing machine after 11 am.",
        "If the breaker trips, call your installer.",
    ],
)
def test_ordinary_wording_is_not_flagged(text):
    assert check_answer(answer(explanation=text)) == []


def test_every_field_is_checked_except_the_data_time():
    assert check_answer(answer(estimated_impact="I have turned off the AC."))
    assert check_answer(answer(data_time="I have turned off the AC.")) == []


# --- tool x agent policy --------------------------------------------------------------------------


def test_the_knowledge_agent_cannot_read_live_telemetry():
    allowed = AGENT_TOOLS[AgentName.KNOWLEDGE]
    assert allowed == {ToolName.SEARCH_KNOWLEDGE, ToolName.HOUSEHOLD_PROFILE}
    assert not is_allowed(AgentName.KNOWLEDGE, ToolName.LIVE_ENERGY_STATE)
    assert not is_allowed(AgentName.KNOWLEDGE, ToolName.DEVICE_READINGS)


def test_the_device_action_agent_has_no_data_tools_at_all():
    assert AGENT_TOOLS[AgentName.DEVICE_ACTION] == frozenset()


def test_every_agent_has_an_explicit_entry():
    assert set(AGENT_TOOLS) == set(AgentName)


def test_an_unauthorised_tool_is_absent_and_refused_loudly():
    tools = AuthorizedTools(AgentName.KNOWLEDGE, {n: object() for n in ToolName})
    assert set(tools) == {ToolName.SEARCH_KNOWLEDGE, ToolName.HOUSEHOLD_PROFILE}
    labels = POLICY_DENIALS.labels(agent="knowledge", tool=ToolName.LIVE_ENERGY_STATE.value)
    before = labels._value.get()
    assert ToolName.LIVE_ENERGY_STATE not in tools  # asking is not calling: no denial is counted
    assert labels._value.get() == before
    with pytest.raises(ToolForbiddenError) as raised:
        tools[ToolName.LIVE_ENERGY_STATE]
    assert raised.value.agent is AgentName.KNOWLEDGE
    assert labels._value.get() == before + 1


def test_an_allowed_tool_is_returned_untouched():
    sentinel = object()
    tools = AuthorizedTools(AgentName.KNOWLEDGE, {ToolName.SEARCH_KNOWLEDGE: sentinel})
    assert tools[ToolName.SEARCH_KNOWLEDGE] is sentinel


def test_an_allowed_but_missing_tool_is_an_ordinary_key_error_not_a_denial():
    tools = AuthorizedTools(AgentName.ENERGY_INSIGHT, {})
    with pytest.raises(KeyError) as raised:
        tools[ToolName.LIVE_ENERGY_STATE]
    assert not isinstance(raised.value, ToolForbiddenError)


@pytest.mark.parametrize("intent", list(Intent))
def test_every_tool_the_planner_selects_for_an_agent_is_one_that_agent_may_call(intent):
    deadlines = {agent: 5.0 for agent in AgentName}
    plan = build_agent_plan(question="q", intents=[intent], appliance=None, deadlines=deadlines)
    for call in plan.calls:
        planned = set(build_plan(Intent(i) for i in call.intents).tools)
        assert planned <= AGENT_TOOLS[call.agent], (
            f"{intent.value}: {call.agent.value} would plan {sorted(t.value for t in planned - AGENT_TOOLS[call.agent])} "
            "which the policy forbids"
        )
