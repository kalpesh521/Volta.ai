"""
The v1/v2 parity harness. It is the only thing that may spend real model calls, so its cap is tested as hard
as any production guarantee: a bug here costs money, not just correctness.
"""
from __future__ import annotations

import copy

import pytest
from httpx import AsyncClient

from app.agents_platform.evals.parity import (
    V1_PATH,
    V2_PATH,
    ModelCallBudget,
    _client_ask,
    calls_incurred,
    compare,
    numbers_in,
    run_parity,
    worst_case_calls,
    wording_overlap,
)
from app.modules.assistant.deps import get_structured_llm
from main import app
from tests.assistant_helpers import FakeStructuredLLM
from tests.test_assistant_api import _home

CONFIDENT = "Why is the battery discharging?"
UNSURE = "something odd is going on with my house"


def response(**overrides) -> dict:
    base = {
        "intents": ["battery"],
        "tools_used": ["get_battery_status", "get_live_energy_state"],
        "analytics_used": ["backup_duration"],
        "confidence": "high",
        "data_freshness": {"status": "fresh"},
        "sources": [],
        "answer": {
            "observation": "The battery is at 62% and discharging 1.5 kW.",
            "explanation": "The home uses more than solar produces.",
            "recommendation": "Delay the washing machine.",
            "estimated_impact": "Saves 0.8 kWh.",
            "data_time": "2026-11-12 10:00",
        },
        "meta": {"llm_used": True, "fallback_reason": None, "classification_method": "rules"},
    }
    return {**copy.deepcopy(base), **overrides}


# --- the budget -----------------------------------------------------------------------------------


def test_a_budget_says_no_before_it_is_spent_not_after():
    budget = ModelCallBudget(3)
    assert budget.can_afford(3) and not budget.can_afford(4)
    budget.charge(2)
    assert budget.can_afford(1) and not budget.can_afford(2)


def test_a_negative_budget_is_refused_and_zero_is_allowed():
    with pytest.raises(ValueError):
        ModelCallBudget(-1)
    assert not ModelCallBudget(0).can_afford(1)


def test_a_question_the_rules_route_costs_one_call_and_an_unsure_one_may_cost_two():
    assert worst_case_calls(CONFIDENT) == 1
    assert worst_case_calls(UNSURE) == 2


def test_the_real_cost_is_read_from_the_responses_own_metadata():
    assert calls_incurred(response()) == 1
    assert calls_incurred(response(meta={"llm_used": False, "classification_method": "rules"})) == 0
    assert calls_incurred(response(meta={"llm_used": True, "classification_method": "llm"})) == 2
    assert calls_incurred({}) == 0


# --- the comparison -------------------------------------------------------------------------------


def test_identical_facts_with_different_wording_still_match():
    other = response()
    other["answer"] = {**other["answer"], "observation": "Battery 62%, draining at 1.5 kW.",
                       "explanation": "Consumption exceeds solar output."}
    report = compare(CONFIDENT, response(), other)
    assert report.matches and report.wording_overlap < 1.0


def test_a_different_number_is_reported_but_does_not_fail_the_run():
    # Both answers passed the grounding gate, so a different number is a different (verified) fact chosen.
    other = response()
    other["answer"] = {**other["answer"], "observation": "The battery is at 63% and discharging 1.5 kW."}
    report = compare(CONFIDENT, response(), other)
    assert [(d.field, d.informational) for d in report.differences] == [("numbers stated", True)]
    assert report.matches


@pytest.mark.parametrize(
    "field,override",
    [
        ("intents", {"intents": ["solar_production"]}),
        ("tools_used", {"tools_used": ["get_battery_status"]}),
        ("confidence", {"confidence": "medium"}),
        ("data_freshness.status", {"data_freshness": {"status": "stale"}}),
        ("meta.llm_used", {"meta": {"llm_used": False, "fallback_reason": None, "classification_method": "rules"}}),
        ("sources", {"sources": [{"document_id": "d1"}]}),
    ],
)
def test_each_fact_field_is_compared(field, override):
    report = compare(CONFIDENT, response(), response(**override))
    assert field in [d.field for d in report.differences]
    assert not report.matches  # and each one fails the run, unlike wording and stated numbers


def test_the_data_time_is_not_counted_as_a_stated_number():
    assert numbers_in({"observation": "62% now", "data_time": "2026-11-12 10:00"}) == {"62"}


def test_wording_overlap_is_one_for_identical_text_and_low_for_unrelated_text():
    a = {"observation": "battery discharging", "data_time": "x"}
    assert wording_overlap(a, a) == 1.0
    assert wording_overlap(a, {"observation": "solar exporting"}) < 0.5


# --- the run --------------------------------------------------------------------------------------


class Recorder:
    def __init__(self, *, router: bool = False) -> None:
        self.paths: list[str] = []
        self._router = router

    async def __call__(self, path: str, question: str) -> dict:
        self.paths.append(path)
        method = "llm" if self._router else "rules"
        return response(meta={"llm_used": True, "fallback_reason": None, "classification_method": method})


async def test_a_pair_costs_two_calls_and_a_budget_of_three_allows_exactly_one_pair():
    ask = Recorder()
    report = await run_parity(ask, ask, [CONFIDENT, "How much solar today?"], ModelCallBudget(3))

    assert len(ask.paths) == 2 and ask.paths == [V1_PATH, V2_PATH]  # the second pair was not even attempted
    assert report.calls_used == 2 and report.skipped == ["How much solar today?"]
    assert report.passed


async def test_an_unsure_question_is_priced_at_its_worst_case_before_it_is_sent():
    ask = Recorder()
    report = await run_parity(ask, ask, [UNSURE], ModelCallBudget(3))  # worst case 2 per endpoint = 4 > 3

    assert ask.paths == [] and report.skipped == [UNSURE] and report.calls_used == 0
    assert not report.passed  # nothing was compared, so nothing was proven


async def test_a_router_call_that_really_happened_is_charged():
    ask = Recorder(router=True)
    report = await run_parity(ask, ask, [CONFIDENT], ModelCallBudget(10))
    assert report.calls_used == 4  # (answer + router) on each endpoint


async def test_a_zero_budget_sends_nothing():
    ask = Recorder()
    report = await run_parity(ask, ask, [CONFIDENT], ModelCallBudget(0))
    assert ask.paths == [] and report.calls_used == 0


async def test_the_harness_works_end_to_end_against_the_real_endpoints_with_a_fake_model(client: AsyncClient):
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    headers, _, _ = await _home(client, "parity-harness@example.com")
    ask = _client_ask(client, headers)

    report = await run_parity(ask, ask, [CONFIDENT], ModelCallBudget(3))

    assert report.passed, [d for q in report.questions for d in q.differences]
    assert report.calls_used == 2 and len(fake.calls) == 2  # the harness's count equals the model's own
