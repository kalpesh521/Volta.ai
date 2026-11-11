"""
The supervisor graph in isolation: stub agents and a scripted model, no HTTP, no database.

These tests pin the behaviour the design promises: parallel dispatch, one LLM call per answer, failure
isolation between agents, the output gate with one regeneration, and per-turn state that does not leak
from one question into the next.
"""
from __future__ import annotations

import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents_platform.contracts import AgentName, AgentResult, AgentStatus, AnswerParts
from app.agents_platform.evals.stubs import (
    CLEAN_DRAFT,
    ScriptedLLM,
    StubAgent,
    default_stub_agents,
    llm_failure,
)
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.state import (
    MAX_STORED_TURNS,
    PERSISTENT_KEYS,
    SupervisorState,
    per_turn_reset,
)
from app.agents_platform.orchestration.supervisor import build_supervisor
from app.ai.config import AISettings, LLMRole
from app.modules.assistant.domain.answers import AnswerDraft
from app.modules.assistant.domain.finalize import STALE_PREFIX

ENERGY_Q = "Why is the battery discharging?"
DOC_Q = "What does fault code E04 mean?"
BOTH_Q = "What does fault code E04 mean and how is my battery?"
CONTROL_Q = "Turn off the geyser"
OFF_TOPIC_Q = "What is the capital of France?"

UNGROUNDED = AnswerDraft(
    observation="The battery is at 87.3% charge.",
    explanation="Because of the weather.",
    recommendation="Wait.",
    estimated_impact="None.",
)
SOC_FACTS = {"battery": {"soc_percent": 62}}


def settings(**overrides) -> AISettings:
    """Defaults that do not depend on the developer's .env."""
    return AISettings(_env_file=None, **{"AI_GROUNDING_MODE": "enforce", **overrides})


class Harness:
    def __init__(self, *, llm: ScriptedLLM | None = None, agents=None, **overrides) -> None:
        self.llm = llm or ScriptedLLM()
        self.agents = agents or default_stub_agents()
        self.runtime = SupervisorRuntime(agents=self.agents, llm=self.llm, settings=settings(**overrides))
        self.graph = build_supervisor(InMemorySaver())
        self.thread = {"configurable": {"thread_id": "t"}}

    async def ask(self, question: str) -> SupervisorState:
        return await self.graph.ainvoke(
            {"question": question, "request_id": "req-1"}, context=self.runtime, config=self.thread
        )

    def answer_calls(self) -> int:
        return self.llm.count(LLMRole.ANSWER)


def energy(h: Harness) -> StubAgent:
    return h.agents[AgentName.ENERGY_INSIGHT]


def knowledge(h: Harness) -> StubAgent:
    return h.agents[AgentName.KNOWLEDGE]


# --- routing and dispatch -------------------------------------------------------------------------


async def test_one_agent_question_makes_one_llm_call_and_stamps_the_data_time() -> None:
    h = Harness()
    state = await h.ask(ENERGY_Q)

    assert [r["agent"] for r in state["results"]] == ["energy_insight"]
    assert h.answer_calls() == 1 and h.llm.count(LLMRole.ROUTER) == 0
    assert state["llm_used"] is True
    assert state["answer"]["observation"] == CLEAN_DRAFT.observation
    assert state["answer"]["data_time"] == "unavailable"
    assert state["grounding"]["mode"] == "enforce"


async def test_a_two_agent_question_runs_both_in_parallel_and_still_makes_one_llm_call() -> None:
    agents = {
        AgentName.ENERGY_INSIGHT: StubAgent(AgentName.ENERGY_INSIGHT, delay=0.3, facts=SOC_FACTS),
        AgentName.KNOWLEDGE: StubAgent(AgentName.KNOWLEDGE, delay=0.3, facts={"knowledge": {"passages": ["E04"]}}),
        AgentName.DEVICE_ACTION: StubAgent(AgentName.DEVICE_ACTION),
    }
    h = Harness(agents=agents)

    started = time.perf_counter()
    state = await h.ask(BOTH_Q)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.55, f"agents ran one after another ({elapsed:.2f}s)"
    assert {r["agent"] for r in state["results"]} == {"energy_insight", "knowledge"}
    assert h.answer_calls() == 1
    prompt = h.llm.prompts(LLMRole.ANSWER)[0]
    assert "soc_percent" in prompt and "E04" in prompt


async def test_results_follow_plan_order_even_when_a_later_agent_finishes_first() -> None:
    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, delay=0.2)
    h = Harness(agents=agents)
    state = await h.ask(BOTH_Q)

    from app.agents_platform.orchestration.nodes import _results

    assert [r.agent.value for r in _results(state)] == ["knowledge", "energy_insight"]


async def test_device_control_is_exclusive_and_never_reaches_the_llm() -> None:
    h = Harness()
    state = await h.ask(CONTROL_Q)

    assert [r["agent"] for r in state["results"]] == ["device_action"]
    assert not energy(h).calls and not knowledge(h).calls
    assert h.answer_calls() == 0
    assert state["llm_used"] is False
    assert state["fallback_reason"] == "device_action"


async def test_an_off_topic_question_runs_no_agent_and_spends_no_answer_tokens() -> None:
    h = Harness()
    state = await h.ask(OFF_TOPIC_Q)

    assert state["results"] == []
    assert h.answer_calls() == 0
    assert state["fallback_reason"] == "out_of_scope"
    assert state["confidence"] == "high"
    assert "outside what I can help" in state["answer"]["observation"]


async def test_an_agent_that_answers_by_itself_is_not_rewritten_by_the_llm() -> None:
    gap = AnswerParts(
        observation="That is not in your documents.",
        explanation="No passage matches.",
        recommendation="Contact your installer.",
        estimated_impact="Informational only.",
    )
    agents = default_stub_agents()
    agents[AgentName.KNOWLEDGE] = StubAgent(AgentName.KNOWLEDGE, draft=gap, fallback_reason="knowledge_gap")
    h = Harness(agents=agents)
    state = await h.ask(DOC_Q)

    assert h.answer_calls() == 0
    assert state["answer"]["observation"] == gap.observation
    assert state["llm_used"] is False


# --- failure isolation ----------------------------------------------------------------------------


async def test_a_slow_agent_times_out_without_blocking_its_sibling() -> None:
    agents = default_stub_agents()
    agents[AgentName.KNOWLEDGE] = StubAgent(AgentName.KNOWLEDGE, delay=5.0)
    h = Harness(agents=agents, AI_KNOWLEDGE_AGENT_DEADLINE_SECONDS=0.05)

    started = time.perf_counter()
    state = await h.ask(BOTH_Q)

    assert time.perf_counter() - started < 1.0
    by_agent = {r["agent"]: r["status"] for r in state["results"]}
    assert by_agent == {"energy_insight": "ok", "knowledge": "timeout"}
    assert state["llm_used"] is True
    assert any("knowledge" in w for w in state["warnings"])
    assert state["confidence"] in ("medium", "low")


async def test_a_crashing_agent_becomes_an_error_result_not_an_exception() -> None:
    agents = default_stub_agents()
    agents[AgentName.KNOWLEDGE] = StubAgent(AgentName.KNOWLEDGE, error=RuntimeError("db down"))
    h = Harness(agents=agents)
    state = await h.ask(BOTH_Q)

    assert {r["agent"]: r["status"] for r in state["results"]}["knowledge"] == "error"
    assert state["llm_used"] is True and state["answer"]["observation"]


async def test_when_every_agent_fails_the_answer_says_so_and_no_tokens_are_spent() -> None:
    agents = {
        AgentName.ENERGY_INSIGHT: StubAgent(AgentName.ENERGY_INSIGHT, error=RuntimeError("x")),
        AgentName.KNOWLEDGE: StubAgent(AgentName.KNOWLEDGE, error=RuntimeError("x")),
        AgentName.DEVICE_ACTION: StubAgent(AgentName.DEVICE_ACTION),
    }
    h = Harness(agents=agents)
    state = await h.ask(BOTH_Q)

    assert h.answer_calls() == 0
    assert state["fallback_reason"] == "agents_unavailable"
    assert state["confidence"] == "low"
    assert "could not load" in state["answer"]["observation"]


# --- output gate and regeneration -----------------------------------------------------------------


async def test_an_ungrounded_answer_is_regenerated_once_with_the_failing_numbers_listed() -> None:
    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, facts=SOC_FACTS)
    h = Harness(llm=ScriptedLLM([UNGROUNDED, CLEAN_DRAFT]), agents=agents)
    state = await h.ask(ENERGY_Q)

    assert h.answer_calls() == 2
    first, second = h.llm.prompts(LLMRole.ANSWER)
    assert "Correction needed" not in first
    assert "Correction needed" in second and "87.3" in second
    assert state["answer"]["observation"] == CLEAN_DRAFT.observation
    assert state["llm_used"] is True
    assert state["grounding"]["attempts"] == 2 and state["grounding"]["ungrounded"] == []


async def test_a_second_ungrounded_answer_falls_back_to_the_verified_draft() -> None:
    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, facts=SOC_FACTS)
    h = Harness(llm=ScriptedLLM([UNGROUNDED, UNGROUNDED]), agents=agents)
    state = await h.ask(ENERGY_Q)

    assert h.answer_calls() == 2  # bounded: never a third attempt
    assert state["llm_used"] is False
    assert state["fallback_reason"] == "ungrounded_numbers"
    assert "87.3" not in " ".join(state["answer"].values())
    assert "Verified summary shown" in state["answer"]["explanation"]


async def test_with_one_attempt_allowed_there_is_no_regeneration() -> None:
    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, facts=SOC_FACTS)
    h = Harness(llm=ScriptedLLM([UNGROUNDED, CLEAN_DRAFT]), agents=agents, AI_ANSWER_MAX_ATTEMPTS=1)
    state = await h.ask(ENERGY_Q)

    assert h.answer_calls() == 1 and state["fallback_reason"] == "ungrounded_numbers"


async def test_observe_mode_keeps_the_model_answer_and_records_the_finding() -> None:
    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, facts=SOC_FACTS)
    h = Harness(llm=ScriptedLLM([UNGROUNDED]), agents=agents, AI_GROUNDING_MODE="observe")
    state = await h.ask(ENERGY_Q)

    assert h.answer_calls() == 1
    assert state["llm_used"] is True and state["answer"]["observation"] == UNGROUNDED.observation
    assert state["grounding"]["ungrounded"] == ["87.3%"]


async def test_a_model_failure_falls_back_to_the_agent_drafts_with_a_note() -> None:
    h = Harness(llm=ScriptedLLM([llm_failure()]))
    state = await h.ask(ENERGY_Q)

    assert state["llm_used"] is False
    assert state["fallback_reason"] == "llm_error"
    assert "without the AI model" in state["answer"]["explanation"]
    assert state["errors"]


async def test_a_stale_reading_adds_the_caution_prefix_and_the_data_time() -> None:
    stale = {
        "status": "stale",
        "data_time": "2026-11-11T08:00:00+00:00",
        "age_seconds": 4000,
        "threshold_seconds": 600,
    }

    def build(call):
        return AgentResult(
            agent=AgentName.ENERGY_INSIGHT,
            status=AgentStatus.OK,
            draft=AnswerParts(**CLEAN_DRAFT.model_dump()),
            freshness=stale,
            live_advice=True,
            confidence="low",
        )

    agents = default_stub_agents()
    agents[AgentName.ENERGY_INSIGHT] = StubAgent(AgentName.ENERGY_INSIGHT, build=build)
    state = await Harness(agents=agents).ask(ENERGY_Q)

    assert state["answer"]["recommendation"].startswith(STALE_PREFIX)
    assert state["answer"]["data_time"].startswith("2026-11-11T08:00:00")
    assert state["confidence"] == "low"


# --- memory and per-turn isolation ----------------------------------------------------------------


async def test_a_follow_up_inherits_the_previous_intents_and_carries_the_earlier_question() -> None:
    h = Harness()
    await h.ask(ENERGY_Q)
    state = await h.ask("why?")

    assert state["classification_method"] == "memory"
    assert state["intents"] == ["battery"]
    assert h.llm.count(LLMRole.ROUTER) == 0
    sent = energy(h).calls[-1].question
    assert sent.startswith("why?") and ENERGY_Q in sent


async def test_a_confident_question_is_never_treated_as_a_follow_up() -> None:
    h = Harness()
    await h.ask(ENERGY_Q)
    state = await h.ask("How much solar did I generate today?")

    assert state["classification_method"] == "rules"
    assert state["intents"] == ["solar_production"]


async def test_a_follow_up_to_a_refusal_does_not_continue_the_refused_topic() -> None:
    h = Harness()
    await h.ask(OFF_TOPIC_Q)
    state = await h.ask("why?")

    assert state["classification_method"] != "memory"


async def test_per_turn_state_does_not_leak_into_the_next_question() -> None:
    agents = default_stub_agents()
    agents[AgentName.KNOWLEDGE] = StubAgent(AgentName.KNOWLEDGE, error=RuntimeError("down"))
    h = Harness(agents=agents)

    first = await h.ask(BOTH_Q)
    assert len(first["results"]) == 2 and first["warnings"]

    second = await h.ask(ENERGY_Q)
    assert [r["agent"] for r in second["results"]] == ["energy_insight"]
    assert second["warnings"] == []
    assert second["errors"] == [] and second["attempts"] == 1
    assert second["fallback_reason"] is None


async def test_memory_is_kept_and_capped() -> None:
    h = Harness()
    for _ in range(MAX_STORED_TURNS + 3):
        state = await h.ask(ENERGY_Q)
    assert len(state["turns"]) == MAX_STORED_TURNS


async def test_threads_are_independent() -> None:
    h = Harness()
    await h.ask(ENERGY_Q)
    other = await h.graph.ainvoke(
        {"question": "why?", "request_id": "r"},
        context=h.runtime,
        config={"configurable": {"thread_id": "someone-else"}},
    )
    assert other["classification_method"] != "memory"


def test_every_state_key_is_either_persistent_or_reset_each_turn() -> None:
    """A new per-turn key that is not reset would silently carry over between questions."""
    declared = set(SupervisorState.__annotations__)
    assert declared == set(per_turn_reset()) | PERSISTENT_KEYS


# --- streaming ------------------------------------------------------------------------------------


async def test_status_events_stream_in_order_before_the_final_state() -> None:
    h = Harness()
    events = []
    async for mode, chunk in h.graph.astream(
        {"question": BOTH_Q, "request_id": "r"},
        context=h.runtime,
        config=h.thread,
        stream_mode=["custom", "values"],
    ):
        if mode == "custom":
            events.append(chunk)

    stages = [e["stage"] for e in events]
    assert stages[0] == "understanding"
    assert stages.count("agent_started") == 2
    assert stages[-1] == "synthesizing"
    assert {e["agent"] for e in events if e["stage"] == "agent_started"} == {"energy_insight", "knowledge"}


@pytest.mark.parametrize("question", [ENERGY_Q, DOC_Q, BOTH_Q, CONTROL_Q, OFF_TOPIC_Q])
async def test_every_path_ends_with_a_complete_answer(question: str) -> None:
    state = await Harness().ask(question)
    assert set(state["answer"]) >= {
        "observation",
        "explanation",
        "recommendation",
        "estimated_impact",
        "data_time",
    }
    assert all(state["answer"][k] for k in ("observation", "recommendation"))
