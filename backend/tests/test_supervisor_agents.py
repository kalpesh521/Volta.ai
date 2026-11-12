"""The specialist agents: how they map their work to an `AgentResult`, and how they fail."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from langchain_core.tools import StructuredTool

from app.agents_platform.agents.device_action.agent import DEVICE_ACTION_REASON, DeviceActionAgent
from app.agents_platform.agents.energy_insight import EnergyInsightAgent
from app.agents_platform.agents.insight_engine import InsightEngine
from app.agents_platform.agents.knowledge import KnowledgeAgent
from app.agents_platform.contracts import AgentCall, AgentName, AgentStatus
from app.agents_platform.evals.stubs import ScriptedLLM
from app.ai.config import AISettings, LLMRole
from app.agents_platform.agents.device_action.schemas import ActionResponse
from app.modules.assistant.tools.registry import NoArgs, ToolName, unavailable


HOUSEHOLD = {
    "system_type": "hybrid",
    "solar_capacity_kwp": 5.0,
    "inverter_capacity_kw": 5.0,
    "battery_present": True,
    "battery_capacity_kwh": 10.0,
    "battery_usable_capacity_kwh": 9.0,
    "battery_minimum_soc_percent": 20.0,
    "grid_available": True,
    "zero_export_mode": False,
    "primary_goal": "reduce_bill",
}


def tool(name: ToolName, fn) -> StructuredTool:
    return StructuredTool.from_function(coroutine=fn, name=name.value, description=name.value, args_schema=NoArgs)


def engine(tools: dict[ToolName, StructuredTool], llm: ScriptedLLM, **overrides) -> InsightEngine:
    return InsightEngine(
        tools_for=lambda agent, question: tools,
        llm=llm,
        settings=AISettings(_env_file=None, **overrides),
        now=lambda: datetime.now(timezone.utc),
    )


def call(agent: AgentName, *intents: str) -> AgentCall:
    return AgentCall(agent=agent, question="q", intents=intents, deadline_seconds=5)


async def test_the_engine_gathers_facts_and_never_calls_the_model():
    async def no_data() -> dict:
        return unavailable("none yet")

    llm = ScriptedLLM()
    tools = {t: tool(t, no_data) for t in ToolName}
    agent = EnergyInsightAgent(engine(tools, llm), request_id="r")

    out = await agent.run(call(AgentName.ENERGY_INSIGHT, "battery"))

    assert llm.calls == [], "writing the answer is the synthesizer's job, not the agent's"
    assert out.agent is AgentName.ENERGY_INSIGHT


async def test_no_telemetry_is_a_partial_result_that_answers_for_itself():
    async def no_data() -> dict:
        return unavailable("none yet")

    agent = EnergyInsightAgent(engine({t: tool(t, no_data) for t in ToolName}, ScriptedLLM()), request_id="r")
    out = await agent.run(call(AgentName.ENERGY_INSIGHT, "battery"))

    assert out.status is AgentStatus.PARTIAL
    assert out.fallback_reason == "no_data" and out.answers_itself
    assert "no energy data" in out.draft.observation.lower()
    assert out.freshness["status"] == "missing"


async def test_a_tool_that_raises_degrades_the_result_instead_of_failing_it():
    async def boom() -> dict:
        raise RuntimeError("store down")

    agent = EnergyInsightAgent(engine({t: tool(t, boom) for t in ToolName}, ScriptedLLM()), request_id="r")
    out = await agent.run(call(AgentName.ENERGY_INSIGHT, "battery"))

    assert out.status is AgentStatus.PARTIAL
    assert {c["status"] for c in out.tool_calls} == {"error"}
    assert any("could not be loaded" in w for w in out.warnings)


async def test_a_slow_tool_times_out_inside_the_agent():
    async def slow() -> dict:
        await asyncio.sleep(5)
        return {}

    agent = EnergyInsightAgent(
        engine({t: tool(t, slow) for t in ToolName}, ScriptedLLM(), AI_TOOL_TIMEOUT_SECONDS=0.05),
        request_id="r",
    )
    out = await agent.run(call(AgentName.ENERGY_INSIGHT, "battery"))

    assert {c["status"] for c in out.tool_calls} == {"timeout"}
    assert out.status is AgentStatus.PARTIAL


async def test_a_document_gap_is_a_deterministic_not_in_your_documents_answer():
    async def knowledge() -> dict:
        return {"available": False, "passages": [], "bill": None, "gap_kind": "fault_code"}

    async def profile() -> dict:
        return unavailable("no profile")

    tools = {t: tool(t, knowledge if t is ToolName.SEARCH_KNOWLEDGE else profile) for t in ToolName}
    llm = ScriptedLLM()
    out = await KnowledgeAgent(engine(tools, llm), request_id="r").run(call(AgentName.KNOWLEDGE, "document"))

    assert out.agent is AgentName.KNOWLEDGE
    assert out.fallback_reason == "knowledge_gap" and out.answers_itself
    assert out.citations == []
    assert "installer" in out.draft.recommendation.lower()
    assert llm.count(LLMRole.ANSWER) == 0


async def test_retrieved_passages_become_citations():
    passage = {"document_id": "d1", "title": "Manual", "doc_type": "inverter_manual", "page": 3, "score": 0.9,
               "excerpt": "E04 means PV overvoltage"}

    async def knowledge() -> dict:
        return {"available": True, "passages": [passage], "bill": None}

    async def profile() -> dict:
        return HOUSEHOLD

    async def nothing() -> dict:
        return unavailable("not needed")

    by_tool = {ToolName.SEARCH_KNOWLEDGE: knowledge, ToolName.HOUSEHOLD_PROFILE: profile}
    tools = {t: tool(t, by_tool.get(t, nothing)) for t in ToolName}
    out = await KnowledgeAgent(engine(tools, ScriptedLLM()), request_id="r").run(
        call(AgentName.KNOWLEDGE, "document")
    )

    assert out.citations == [passage]
    assert out.fallback_reason is None and not out.answers_itself
    assert "E04" in str(out.facts), "the passage reaches the facts the model will see"


# --- device action --------------------------------------------------------------------------------


class FakeProposer:
    def __init__(self, response: ActionResponse) -> None:
        self.response = response
        self.calls: list[tuple[uuid.UUID, str]] = []

    async def propose(self, system, user_id, question):
        self.calls.append((user_id, question))
        return self.response


def action(status: str, **kw) -> ActionResponse:
    return ActionResponse(proposal_id="p1", status=status, message=kw.pop("message", "msg"), **kw)


async def run_action(response: ActionResponse):
    proposer = FakeProposer(response)
    user_id = uuid.uuid4()
    agent = DeviceActionAgent(proposer, system=SimpleNamespace(), user_id=user_id)  # type: ignore[arg-type]
    out = await agent.run(call(AgentName.DEVICE_ACTION, "device_control"))
    return out, proposer, user_id


async def test_a_pending_proposal_is_reported_as_ok_and_never_as_an_executed_action():
    expires = datetime.now(timezone.utc) + timedelta(seconds=90)
    out, proposer, user_id = await run_action(
        action("awaiting_confirmation", message="I can queue turning on the water heater.",
               device="water_heater", command="on", expires_at=expires)
    )

    assert out.status is AgentStatus.OK and out.answers_itself
    assert out.fallback_reason == DEVICE_ACTION_REASON
    assert out.proposals[0]["proposal_id"] == "p1" and out.proposals[0]["status"] == "awaiting_confirmation"
    assert out.draft.observation == "I can queue turning on the water heater."
    assert "Nothing has changed" in out.draft.explanation
    assert "within" in out.draft.recommendation and "seconds" in out.draft.recommendation
    assert "None until you confirm" in out.draft.estimated_impact
    assert proposer.calls == [(user_id, "q")], "the agent acts for the caller, never another user"


async def test_a_rejected_proposal_is_a_refusal_that_says_nothing_was_sent():
    out, _, _ = await run_action(action("rejected", message="Device control is switched off.", reasons=["kill_switch"]))

    assert out.status is AgentStatus.REFUSED
    assert "no command was sent" in out.draft.explanation
    assert "No device was changed" in out.draft.estimated_impact
    assert out.confidence == "high" and out.live_advice is False
