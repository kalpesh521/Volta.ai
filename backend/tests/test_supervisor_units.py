"""Pure pieces of the supervisor: planner, follow-up resolution, result merging."""
from __future__ import annotations

import itertools

import pytest

from app.agents_platform.contracts import (
    MAX_AGENTS_PER_PLAN,
    AgentName,
    AgentResult,
    AgentStatus,
    AnswerParts,
)
from app.agents_platform.orchestration.memory import looks_like_follow_up, resolve_follow_up
from app.agents_platform.orchestration.planner import build_agent_plan
from app.agents_platform.orchestration.state import Turn
from app.agents_platform.orchestration.synthesis import (
    combine_confidence,
    merge_drafts,
    merge_freshness,
    prompt_payload,
)
from app.modules.assistant.domain.answers import FallbackReason
from app.modules.assistant.domain.intents import (
    ClassificationMethod,
    Intent,
    IntentMatch,
    classify_by_rules,
)

DEADLINES = {
    AgentName.ENERGY_INSIGHT: 8.0,
    AgentName.KNOWLEDGE: 10.0,
    AgentName.DEVICE_ACTION: 8.0,
}


def plan(*intents: Intent, appliance: str | None = None):
    return build_agent_plan(question="q", intents=intents, appliance=appliance, deadlines=DEADLINES)


# --- planner --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intents", "agents", "route"),
    [
        ((Intent.BATTERY,), ["energy_insight"], "single"),
        ((Intent.DOCUMENT,), ["knowledge"], "single"),
        ((Intent.SAVINGS, Intent.WEATHER), ["energy_insight"], "single"),
        ((Intent.DOCUMENT, Intent.BATTERY), ["knowledge", "energy_insight"], "multi"),
        ((Intent.BATTERY, Intent.DOCUMENT), ["energy_insight", "knowledge"], "multi"),
        ((Intent.DEVICE_CONTROL,), ["device_action"], "exclusive"),
        ((Intent.OUT_OF_SCOPE,), [], "refusal"),
    ],
)
def test_the_planner_maps_intents_to_agents(intents, agents, route):
    built = plan(*intents)
    assert [c.agent.value for c in built.calls] == agents
    assert built.route == route


def test_one_agent_receives_all_of_its_intents_in_a_single_call():
    built = plan(Intent.SAVINGS, Intent.WEATHER, Intent.DOCUMENT)
    energy = next(c for c in built.calls if c.agent is AgentName.ENERGY_INSIGHT)
    assert energy.intents == ("savings", "weather")


def test_device_control_drops_every_other_intent():
    built = plan(Intent.DEVICE_CONTROL, Intent.BATTERY, Intent.DOCUMENT)
    assert [c.agent for c in built.calls] == [AgentName.DEVICE_ACTION]
    assert built.calls[0].intents == ("device_control",)


def test_a_secondary_control_or_off_topic_intent_never_adds_an_agent():
    built = plan(Intent.BATTERY, Intent.DEVICE_CONTROL, Intent.OUT_OF_SCOPE)
    assert [c.agent for c in built.calls] == [AgentName.ENERGY_INSIGHT]


def test_only_the_energy_agent_gets_the_appliance_and_every_agent_gets_its_own_deadline():
    built = plan(Intent.DOCUMENT, Intent.APPLIANCE_TIMING, appliance="water_heater")
    by_agent = {c.agent: c for c in built.calls}
    assert by_agent[AgentName.ENERGY_INSIGHT].appliance == "water_heater"
    assert by_agent[AgentName.KNOWLEDGE].appliance is None
    assert by_agent[AgentName.KNOWLEDGE].deadline_seconds == 10.0
    assert by_agent[AgentName.ENERGY_INSIGHT].deadline_seconds == 8.0


def test_no_combination_of_intents_breaks_the_plan_invariants():
    for size in (1, 2, 3):
        for combo in itertools.permutations(Intent, size):
            built = plan(*combo)
            agents = [c.agent for c in built.calls]
            assert len(agents) <= MAX_AGENTS_PER_PLAN
            assert len(set(agents)) == len(agents), "an agent is called at most once"
            if AgentName.DEVICE_ACTION in agents:
                assert agents == [AgentName.DEVICE_ACTION], "the exclusive agent never shares a plan"
            assert bool(built.calls) != bool(built.refusal)


# --- follow-ups -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["why?", "Why", "but why?", "and tomorrow?", "What about the washing machine?", "how about at night",
     "is that normal?", "tell me more", "explain", "what if it rains", "and the grid?"],
)
def test_short_elliptical_questions_read_as_follow_ups(question):
    assert looks_like_follow_up(question)


@pytest.mark.parametrize(
    "question",
    ["why is the sky blue", "What is the capital of France?", "How much solar did I generate today?",
     "Why am I importing grid energy even though the battery is nearly full and the sun is out?"],
)
def test_self_contained_or_long_questions_are_not_follow_ups(question):
    assert not looks_like_follow_up(question)


def turn(*intents: str, question="Why is the battery discharging?", appliance=None) -> Turn:
    return Turn(question=question, intents=list(intents), appliance=appliance, agents=["energy_insight"])


def test_a_follow_up_inherits_intents_and_quotes_the_earlier_question():
    follow = resolve_follow_up("why?", classify_by_rules("why?"), [turn("battery", "backup")])

    assert follow is not None
    assert [i.value for i in follow.match.intents] == ["battery", "backup"]
    assert follow.match.method is ClassificationMethod.MEMORY and follow.match.confident
    assert follow.question.startswith("why?") and "Why is the battery discharging?" in follow.question


def test_a_confident_question_is_never_a_follow_up():
    rules = classify_by_rules("and how much solar today?")
    assert rules.confident
    assert resolve_follow_up("and how much solar today?", rules, [turn("battery")]) is None


def test_naming_a_new_subject_is_not_a_follow_up_even_when_it_starts_with_and():
    """"and the grid?" names a domain word. Continuing the battery topic would answer the wrong question."""
    rules = classify_by_rules("and the grid?")
    assert looks_like_follow_up("and the grid?") and rules.primary is Intent.LIVE_OVERVIEW
    assert resolve_follow_up("and the grid?", rules, [turn("battery")]) is None


def test_there_is_nothing_to_follow_without_history():
    assert resolve_follow_up("why?", classify_by_rules("why?"), []) is None


@pytest.mark.parametrize("topic", ["device_control", "out_of_scope"])
def test_a_refusal_or_an_action_is_not_a_topic_to_continue(topic):
    assert resolve_follow_up("why?", classify_by_rules("why?"), [turn(topic)]) is None


def test_the_appliance_carries_over_unless_the_user_names_another():
    previous = [turn("appliance_timing", appliance="water_heater")]

    kept = resolve_follow_up("and tomorrow?", classify_by_rules("and tomorrow?"), previous)
    switched = resolve_follow_up(
        "what about the washing machine?", classify_by_rules("what about the washing machine?"), previous
    )

    assert kept is not None and kept.match.appliance.value == "water_heater"
    assert switched is not None and switched.match.appliance.value == "washing_machine"


def test_a_very_long_earlier_question_is_truncated_in_the_quote():
    follow = resolve_follow_up("why?", classify_by_rules("why?"), [turn("battery", question="x" * 900)])
    assert len(follow.question) < 300


# --- merging --------------------------------------------------------------------------------------


def result(agent=AgentName.ENERGY_INSIGHT, **kw) -> AgentResult:
    return AgentResult(agent=agent, status=kw.pop("status", AgentStatus.OK), **kw)


def fresh(status: str, data_time: str | None) -> dict:
    return {"status": status, "data_time": data_time, "age_seconds": 0, "threshold_seconds": 600}


def test_merged_freshness_is_the_oldest_real_reading_and_ignores_agents_that_read_no_telemetry():
    newest = result(freshness=fresh("fresh", "2026-11-11T10:00:00+00:00"))
    oldest = result(freshness=fresh("stale", "2026-11-11T06:00:00+00:00"))
    no_telemetry = result(AgentName.KNOWLEDGE, freshness=fresh("missing", None))

    merged = merge_freshness([newest, no_telemetry, oldest], threshold_seconds=600)

    assert merged.data_time.isoformat() == "2026-11-11T06:00:00+00:00"
    assert merged.status.value == "stale"


def test_knowledge_alone_does_not_invent_a_data_time():
    merged = merge_freshness([result(AgentName.KNOWLEDGE, freshness=fresh("missing", None))], threshold_seconds=600)
    assert merged.data_time is None and merged.status.value == "missing"
    assert merge_freshness([], threshold_seconds=600).threshold_seconds == 600


def test_confidence_is_the_weakest_and_a_failed_agent_caps_it_at_medium():
    high, low = result(confidence="high"), result(AgentName.KNOWLEDGE, confidence="low")
    assert combine_confidence([high, high], degraded=False) == "high"
    assert combine_confidence([high, low], degraded=False) == "low"
    assert combine_confidence([high], degraded=True) == "medium"
    assert combine_confidence([], degraded=True) == "low"


def parts(tag: str) -> AnswerParts:
    return AnswerParts(
        observation=f"obs {tag}", explanation=f"why {tag}", recommendation=f"do {tag}", estimated_impact=f"gain {tag}"
    )


def test_a_single_draft_passes_through_untouched():
    only = result(draft=parts("a"), fallback_reason="knowledge_gap")
    assert merge_drafts([only]) == parts("a")


def test_several_drafts_are_joined_without_repeating_a_sentence():
    a, b = result(draft=parts("a")), result(AgentName.KNOWLEDGE, draft=parts("b"))
    merged = merge_drafts([a, b])
    assert merged.observation == "obs a obs b" and merged.recommendation == "do a do b"
    twin = merge_drafts([a, a])
    assert twin.observation == "obs a"


def test_the_fallback_note_is_added_once_and_only_when_a_draft_lacks_its_own_reason():
    plain = result(draft=parts("a"))
    own = result(AgentName.KNOWLEDGE, draft=parts("b"), fallback_reason="knowledge_gap")

    noted = merge_drafts([plain, own], reason=FallbackReason.UNGROUNDED)
    assert noted.explanation.count("Verified summary shown") == 1

    both_own = merge_drafts([own, own], reason=FallbackReason.UNGROUNDED)
    assert "Verified summary shown" not in both_own.explanation


def test_the_prompt_payload_merges_facts_and_trims_optional_detail_to_the_budget():
    hours = [{"hour": h, "solar_kwh": 1.25, "load_kwh": 0.75} for h in range(24)]
    energy = result(
        facts={"battery": {"soc_percent": 62}, "hourly_summary": {"hours": hours, "peak_hour": 12}},
        analytics={"solar_surplus": {"state": "deficit"}},
    )
    docs = result(AgentName.KNOWLEDGE, facts={"knowledge": {"passages": [{"excerpt": "E04 means PV overvoltage"}]}})

    roomy, _, _ = prompt_payload([energy, docs], max_facts_chars=50_000, threshold_seconds=600)
    tight, analytics, freshness = prompt_payload([energy, docs], max_facts_chars=400, threshold_seconds=600)

    assert "hours" in roomy["hourly_summary"]
    assert "hours" not in tight["hourly_summary"], "optional detail goes first when over budget"
    assert tight["battery"] == {"soc_percent": 62} and "E04" in str(tight["knowledge"]), "core facts stay"
    assert analytics["solar_surplus"]["state"] == "deficit" and freshness["status"] == "missing"


def test_intent_match_is_unchanged_for_confident_rules():
    """Guards the contract the follow-up logic depends on."""
    match = classify_by_rules("Why is the battery discharging?")
    assert isinstance(match, IntentMatch) and match.confident
