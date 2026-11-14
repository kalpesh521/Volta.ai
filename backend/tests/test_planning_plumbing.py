"""
The shared plumbing for planning agents: intent detection, planning, bounded retry, plan-tier entitlements, data
scopes. Engines (forecast, optimiser, tariff) have their own test files; this pins how the supervisor treats them.
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents_platform.budgets.tiers import Tier
from app.agents_platform.contracts import AgentName, AgentStatus
from app.agents_platform.evals.stubs import ScriptedLLM, StubAgent, default_stub_agents
from app.agents_platform.guardrails.policy import (
    AGENT_DATA,
    AGENT_TOOLS,
    DataForbiddenError,
    DataScope,
    require_scope,
)
from app.agents_platform.orchestration.planner import PLANNING_AGENTS, build_agent_plan
from app.agents_platform.orchestration.planning_intents import (
    PlanningIntent,
    detect_planning_intents,
    merge_intents,
    parse_intent,
)
from app.agents_platform.orchestration.retry import (
    RETRYABLE_AGENTS,
    TransientAgentError,
    max_attempts,
    run_with_retry,
)
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.supervisor import build_supervisor
from app.agents_platform.registry.entitlements import EntitlementRules, Entitlements, parse_plus_only
from app.ai.config import AISettings
from app.core.metrics import AGENT_RETRIES, POLICY_DENIALS
from app.modules.assistant.domain.intents import Intent, classify_by_rules

F, O, B = PlanningIntent.FORECAST, PlanningIntent.OPTIMIZE, PlanningIntent.BILL_ESTIMATE


def detect(question: str) -> tuple[PlanningIntent, ...]:
    return detect_planning_intents(question, classify_by_rules(question))


# --- intent detection -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question, expected",
    [
        ("How much solar will I generate tomorrow?", (F,)),
        ("Will I have enough solar this afternoon?", (F,)),
        ("Forecast my consumption for the next 24 hours", (F,)),
        ("Optimize my battery usage", (O,)),
        ("Plan my day to reduce my bill", (O,)),
        ("How should I use my battery tomorrow?", (O,)),
        ("What if I add 2 kWp of solar, how much would my bill drop?", (B,)),
        ("Estimate my bill next month", (B,)),
        ("Is it worth getting a bigger battery? payback", (B,)),
    ],
)
def test_planning_questions_are_recognised_by_rules(question, expected):
    assert detect(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Can I ignore the battery safety reserve tonight?",  # a future word without a prediction cue
        "Why is the battery discharging?",
        "How long will my battery last in a power cut?",
        "Is the cloudy weather hurting output?",
        "What is the weather forecast tomorrow?",
        "Check my electricity bill and tell me my savings",
        "Explain my electricity bill",
        "Show the tariff order from MERC",
        "How much money did I save today?",
        "and tomorrow?",
        "What is the capital of France?",
    ],
)
def test_ordinary_questions_are_not_taken_over(question):
    assert detect(question) == ()


@pytest.mark.parametrize(
    "question",
    ["Turn off the AC", "Turn on the geyser to optimize my bill", "Can I run the geyser now?", "Give me a full report"],
)
def test_a_device_action_a_named_appliance_verdict_and_an_overview_keep_their_own_path(question):
    assert detect(question) == ()


def test_the_topic_a_planning_agent_already_covers_does_not_add_a_second_agent():
    # "how much solar will I generate tomorrow?" also matches the legacy solar rule; the forecast covers it.
    assert merge_intents("how much solar will i generate tomorrow?", (F,), (Intent.SOLAR_PRODUCTION,)) == (F,)


def test_a_two_part_question_keeps_the_legacy_intents_planning_first_capped_and_generic_ones_dropped():
    legacy = (Intent.SAVINGS, Intent.LIVE_OVERVIEW, Intent.BATTERY, Intent.WEATHER)
    merged = merge_intents("optimize my usage and why is the battery low?", (O,), legacy)
    assert merged == (O, Intent.SAVINGS, Intent.BATTERY)
    assert merge_intents("forecast solar; and the bill?", (F, B), (Intent.OUT_OF_SCOPE,)) == (F, B)


def test_a_document_intent_stays_only_when_the_question_really_asks_for_a_document():
    docs = (Intent.DOCUMENT,)
    assert merge_intents("forecast solar and check the manual", (F,), docs) == (F, Intent.DOCUMENT)
    assert merge_intents("what does my warranty say about forecast", (F,), docs) == (F, Intent.DOCUMENT)
    # the legacy rules call every "bill" a document question; a bill estimate is not one
    assert merge_intents("estimate my bill next month", (B,), docs) == (B,)


def test_an_intent_in_graph_state_parses_back_to_its_kind():
    assert parse_intent("battery") is Intent.BATTERY and parse_intent("forecast") is F
    with pytest.raises(ValueError):
        parse_intent("nonsense")


# --- planning -------------------------------------------------------------------------------------


def plan(intents, appliance=None):
    deadlines = {a: 5.0 for a in AgentName}
    return build_agent_plan(question="q", intents=intents, appliance=appliance, deadlines=deadlines)


def test_each_planning_intent_goes_to_its_own_agent():
    assert PLANNING_AGENTS == {F: AgentName.FORECAST, O: AgentName.OPTIMIZER, B: AgentName.BILL_TARIFF}
    for intent, agent in PLANNING_AGENTS.items():
        built = plan((intent,))
        assert [c.agent for c in built.calls] == [agent] and built.route == "single"


def test_a_mixed_question_runs_the_planning_agent_beside_energy_insight_in_parallel():
    built = plan((O, Intent.BATTERY))
    assert [c.agent for c in built.calls] == [AgentName.OPTIMIZER, AgentName.ENERGY_INSIGHT]
    assert built.route == "multi" and not built.exclusive


def test_only_energy_insight_and_the_optimizer_are_told_the_appliance():
    built = plan((O, Intent.BATTERY), appliance="water_heater")
    assert {c.agent: c.appliance for c in built.calls} == {
        AgentName.OPTIMIZER: "water_heater",
        AgentName.ENERGY_INSIGHT: "water_heater",
    }
    assert plan((F,), appliance="water_heater").calls[0].appliance is None


def test_device_control_stays_exclusive_whatever_planning_intent_comes_with_it():
    built = plan((Intent.DEVICE_CONTROL, O))
    assert built.exclusive and [c.agent for c in built.calls] == [AgentName.DEVICE_ACTION]


def test_a_plan_never_has_more_agents_than_the_cap():
    assert len(plan((F, O, B)).calls) <= 4


# --- policy ---------------------------------------------------------------------------------------


def test_every_agent_has_a_tool_entry_and_every_planning_agent_has_data_scopes():
    assert set(AGENT_TOOLS) == set(AgentName)
    for agent in PLANNING_AGENTS.values():
        assert AGENT_DATA[agent], f"{agent.value} reads data but has no scope"
        assert not AGENT_TOOLS[agent], "planning agents compute; they do not call LLM-facing tools"


def test_a_scope_outside_an_agents_job_is_refused_and_counted():
    before = POLICY_DENIALS.labels(agent="bill_tariff", tool="data:live")._value.get()
    with pytest.raises(DataForbiddenError):
        require_scope(AgentName.BILL_TARIFF, DataScope.LIVE)
    assert POLICY_DENIALS.labels(agent="bill_tariff", tool="data:live")._value.get() == before + 1
    require_scope(AgentName.BILL_TARIFF, DataScope.HISTORY)  # its own scope passes


def test_agents_that_read_no_planning_data_have_no_scopes():
    for agent in (AgentName.ENERGY_INSIGHT, AgentName.KNOWLEDGE, AgentName.DEVICE_ACTION):
        with pytest.raises(DataForbiddenError):
            require_scope(agent, DataScope.HISTORY)


# --- retry ----------------------------------------------------------------------------------------


class Flaky:
    def __init__(self, fail_times: int, error: Exception = TransientAgentError("provider 503")) -> None:
        self.fail_times, self.error, self.calls = fail_times, error, 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return "ok"


async def no_sleep(_: float) -> None:
    return None


async def test_a_transient_failure_is_retried_and_counted():
    flaky = Flaky(1)
    before = AGENT_RETRIES.labels(agent="forecast")._value.get()
    result = await run_with_retry(flaky, agent=AgentName.FORECAST, attempts=3, backoff_seconds=0.1, sleep=no_sleep)
    assert result == "ok" and flaky.calls == 2
    assert AGENT_RETRIES.labels(agent="forecast")._value.get() == before + 1


async def test_the_last_attempt_raises_instead_of_looping_forever():
    flaky = Flaky(10)
    with pytest.raises(TransientAgentError):
        await run_with_retry(flaky, agent=AgentName.FORECAST, attempts=3, backoff_seconds=0, sleep=no_sleep)
    assert flaky.calls == 3


@pytest.mark.parametrize("error", [RuntimeError("bug"), ValueError("bad"), TimeoutError()])
async def test_only_a_transient_error_is_retried(error):
    flaky = Flaky(5, error)
    with pytest.raises(type(error)):
        await run_with_retry(flaky, agent=AgentName.FORECAST, attempts=3, backoff_seconds=0, sleep=no_sleep)
    assert flaky.calls == 1


async def test_the_pause_doubles_between_attempts():
    pauses: list[float] = []

    async def record(seconds: float) -> None:
        pauses.append(seconds)

    with pytest.raises(TransientAgentError):
        await run_with_retry(Flaky(9), agent=AgentName.OPTIMIZER, attempts=4, backoff_seconds=0.25, sleep=record)
    assert pauses == [0.25, 0.5, 1.0]


def test_only_planning_agents_may_retry_and_device_action_never_does():
    assert RETRYABLE_AGENTS == {AgentName.FORECAST, AgentName.OPTIMIZER, AgentName.BILL_TARIFF}
    settings = AISettings(_env_file=None, AI_PLANNING_AGENT_MAX_ATTEMPTS=3)
    assert max_attempts(AgentName.DEVICE_ACTION, settings) == 1
    assert max_attempts(AgentName.ENERGY_INSIGHT, settings) == 1
    assert max_attempts(AgentName.FORECAST, settings) == 3
    assert max_attempts(AgentName.FORECAST, AISettings(_env_file=None, AI_PLANNING_AGENT_MAX_ATTEMPTS=0)) == 1


# --- entitlements ---------------------------------------------------------------------------------


def test_the_plus_only_list_is_checked_against_the_closed_agent_set():
    assert parse_plus_only("optimizer, forecast") == {AgentName.OPTIMIZER, AgentName.FORECAST}
    assert parse_plus_only("") == frozenset()
    with pytest.raises(ValueError, match="unknown agents"):
        parse_plus_only("optimiser")  # the typo that would otherwise leave the agent open to everyone


def test_free_users_are_held_back_only_where_the_rules_say_so():
    rules = EntitlementRules(frozenset({AgentName.OPTIMIZER}), what_if_plus_only=True)
    free, plus = rules.for_tier(Tier.FREE), rules.for_tier(Tier.PLUS)
    assert not free.allows(AgentName.OPTIMIZER) and free.allows(AgentName.FORECAST) and free.allows(AgentName.BILL_TARIFF)
    assert plus.allows(AgentName.OPTIMIZER) and plus.what_if_allowed and not free.what_if_allowed
    assert Entitlements().allows(AgentName.OPTIMIZER)  # the default (internal callers, tests) allows everything


def test_an_unknown_tier_is_the_free_tier_so_it_can_never_unlock_a_paid_feature():
    rules = EntitlementRules(frozenset({AgentName.OPTIMIZER}))
    assert not rules.for_tier(Tier.parse("platinum")).allows(AgentName.OPTIMIZER)
    assert not rules.for_tier(Tier.parse(None)).allows(AgentName.OPTIMIZER)


def test_the_default_settings_hold_the_optimizer_for_plus():
    assert EntitlementRules.from_settings(AISettings(_env_file=None)).plus_only == {AgentName.OPTIMIZER}


# --- the graph ------------------------------------------------------------------------------------


class Harness:
    def __init__(self, entitlements: Entitlements = Entitlements(), **overrides) -> None:
        self.agents = default_stub_agents()
        self.llm = ScriptedLLM()
        settings = AISettings(_env_file=None, **{"AI_AGENT_RETRY_BACKOFF_SECONDS": 0.0, **overrides})
        self.runtime = SupervisorRuntime(
            agents=self.agents, llm=self.llm, settings=settings, entitlements=entitlements
        )
        self.graph = build_supervisor(InMemorySaver())

    async def ask(self, question: str, thread: str = "t") -> dict:
        return await self.graph.ainvoke(
            {"question": question, "request_id": "r"},
            context=self.runtime,
            config={"configurable": {"thread_id": thread}},
        )


def ran(state: dict) -> list[str]:
    return [r["agent"] for r in state["results"]]


async def test_a_planning_question_reaches_its_agent_through_the_whole_graph():
    h = Harness()
    state = await h.ask("How much solar will I generate tomorrow?")
    assert ran(state) == ["forecast"] and state["intents"] == ["forecast"]
    assert h.agents[AgentName.FORECAST].calls and not h.agents[AgentName.ENERGY_INSIGHT].calls


async def test_a_free_user_asking_for_a_plus_agent_gets_fixed_wording_and_the_agent_never_runs():
    rules = EntitlementRules(frozenset({AgentName.OPTIMIZER}))
    h = Harness(rules.for_tier(Tier.FREE))
    state = await h.ask("Optimize my battery usage")

    assert h.agents[AgentName.OPTIMIZER].calls == [], "a refused agent must not read data or run"
    assert state["results"][0]["status"] == AgentStatus.REFUSED.value
    assert state["fallback_reason"] == "plan_required" and "Plus plan" in state["answer"]["observation"]
    assert h.llm.calls == [], "no model is asked to write a refusal"
    assert state["confidence"] == "high"


async def test_the_same_question_runs_normally_for_a_plus_user():
    rules = EntitlementRules(frozenset({AgentName.OPTIMIZER}))
    h = Harness(rules.for_tier(Tier.PLUS))
    state = await h.ask("Optimize my battery usage")
    assert h.agents[AgentName.OPTIMIZER].calls and state["results"][0]["status"] == "ok"


async def test_in_a_mixed_question_the_free_part_is_still_answered_and_the_user_is_told_what_was_held_back():
    rules = EntitlementRules(frozenset({AgentName.OPTIMIZER}))
    h = Harness(rules.for_tier(Tier.FREE))
    state = await h.ask("Optimize my battery usage and also why is the battery discharging?")
    assert sorted(ran(state)) == ["energy_insight", "optimizer"]
    assert any("Plus plan" in w for w in state["warnings"])


class TransientThenOk(StubAgent):
    def __init__(self, name: AgentName, fail_times: int) -> None:
        super().__init__(name)
        self.fail_times = fail_times

    async def run(self, call, *, config=None):
        if len(self.calls) < self.fail_times:
            self.calls.append(call)
            raise TransientAgentError("provider 503")
        return await StubAgent.run(self, call, config=config)  # records the call itself


async def test_a_planning_agent_that_fails_transiently_is_retried_inside_the_graph():
    h = Harness()
    flaky = TransientThenOk(AgentName.FORECAST, fail_times=1)
    h.agents[AgentName.FORECAST] = flaky
    state = await h.ask("How much solar will I generate tomorrow?")
    assert len(flaky.calls) == 2 and state["results"][0]["status"] == "ok"


async def test_retries_stay_inside_the_agents_one_deadline():
    class Slow(StubAgent):
        async def run(self, call, *, config=None):
            self.calls.append(call)
            await asyncio.sleep(0.05)
            raise TransientAgentError("still failing")

    h = Harness(AI_FORECAST_AGENT_DEADLINE_SECONDS=0.12, AI_PLANNING_AGENT_MAX_ATTEMPTS=50)
    h.agents[AgentName.FORECAST] = Slow(AgentName.FORECAST)
    loop = asyncio.get_running_loop()
    started = loop.time()
    state = await h.ask("How much solar will I generate tomorrow?")
    assert loop.time() - started < 1.0, "fifty attempts must not each get a fresh deadline"
    assert state["results"][0]["status"] == AgentStatus.TIMEOUT.value
    assert len(h.agents[AgentName.FORECAST].calls) < 10


async def test_device_action_is_never_retried_even_for_a_transient_error():
    h = Harness()
    flaky = TransientThenOk(AgentName.DEVICE_ACTION, fail_times=5)
    h.agents[AgentName.DEVICE_ACTION] = flaky
    state = await h.ask("Turn off the geyser")
    assert len(flaky.calls) == 1 and state["results"][0]["status"] == AgentStatus.ERROR.value


async def test_a_follow_up_to_a_planning_question_keeps_the_planning_topic():
    h = Harness()
    first = await h.ask("How much solar will I generate tomorrow?", thread="follow")
    second = await h.ask("why?", thread="follow")
    assert first["intents"] == ["forecast"] and second["intents"] == ["forecast"]
    assert second["classification_method"] == "memory" and ran(second) == ["forecast"]
