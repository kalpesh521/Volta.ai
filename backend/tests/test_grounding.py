"""Numeric grounding: the checker, and the assistant's behaviour in each mode."""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient

from app.ai.config import get_ai_settings
from app.modules.assistant.deps import get_assistant_settings, get_structured_llm
from app.modules.assistant.domain.answers import AnswerDraft
from app.modules.assistant.domain.grounding import check_grounding, collect_fact_numbers
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding

FACTS = [
    {"solar_kw": 3.2, "load_kw": 1.4, "battery": {"soc_percent": 62.0}, "when": "2026-10-02T18:16:43+05:30"},
    {"surplus_kw": 1.8, "backup_hours": 4.2, "savings_inr": 38.75},
    {"status": "fresh", "age_seconds": 2520},
]


def _check(text: str, *, question: str = "", field: str = "observation"):
    return check_grounding({field: text}, facts=FACTS, question=question)


@pytest.mark.parametrize(
    "text",
    [
        "Solar is producing 3.2 kW while the home uses 1.4 kW.",
        "Battery is at 62%.",
        "That is roughly 3 kW of solar.",  # rounding of 3.2
        "Solar is 3.20 kW.",  # more digits than the fact
        "About 1,800 W is spare.",  # kW to W
        "Backup lasts about 4 hours.",  # rounding of 4.2
        "The data is 42 minutes old.",  # seconds to minutes
        "You saved ₹38.75 today.",
        "You saved Rs. 38.75 today.",
        "A reading from 18:16 on 2026-10-02.",  # times and dates carry no energy claim
        "Run it around 3 PM or 11:30.",
        "Step 1: wait. You have 2 devices on.",  # small unit-less counts
    ],
)
def test_numbers_that_trace_to_the_facts_pass(text):
    result = _check(text)
    assert result.ok, result.ungrounded


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Solar is producing 4.5 kW.", "4.5 kW"),
        ("Battery is at 70%.", "70%"),
        ("The surplus is 3.2 minus 1.4 = 2.5 kW.", "2.5 kW"),  # the model's own arithmetic
        ("Backup lasts 6 hours.", "6 hours"),
        ("You saved ₹120 today.", "120"),
        ("Fixing it costs 5,000 rupees.", "5,000"),
    ],
)
def test_numbers_that_are_not_in_the_facts_are_flagged(text, expected):
    result = _check(text)
    assert not result.ok
    assert expected in result.ungrounded


def test_a_number_the_user_typed_may_be_repeated_back():
    assert _check("Yes, a 2.5 kW heater fits.", question="Can I run a 2.5 kW heater?").ok
    assert not _check("Yes, a 2.5 kW heater fits.").ok


def test_ids_and_model_numbers_are_not_read_as_measurements():
    result = check_grounding(
        {"observation": "Fault E04 on the Growatt SPF 5000."},
        facts=[{"fault": "Growatt SPF 5000 E04"}],
    )
    assert result.ok and result.checked == 1  # only "5000" is a claim, and it is in the facts


def test_every_answer_field_is_checked():
    answer = {"observation": "ok", "explanation": "ok", "recommendation": "ok", "estimated_impact": "Saves ₹999."}
    result = check_grounding(answer, facts=FACTS)
    assert result.ungrounded == ("999",)


def test_repeated_offenders_are_reported_once():
    assert _check("4.5 kW now and 4.5 kW later.").ungrounded == ("4.5 kW",)


def test_numbers_inside_strings_and_nested_structures_are_collected():
    assert collect_fact_numbers({"a": [{"b": "Pune 411001"}], "c": True, "d": None, "e": -2.5}) >= {
        411001.0,
        2.5,
    }


# --- through the HTTP API ---------------------------------------------------------------

INVENTED = AnswerDraft(
    observation="Solar is producing 9.9 kW right now.",
    explanation="That is far above the usual output.",
    recommendation="Run everything.",
    estimated_impact="You will save ₹4,321 today.",
)


@pytest.fixture
def invented_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM(draft=INVENTED)
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


def _use_mode(mode: str) -> None:
    settings = get_ai_settings().model_copy(update={"AI_GROUNDING_MODE": mode})
    app.dependency_overrides[get_assistant_settings] = lambda: settings


async def _ask_live(client: AsyncClient, email: str) -> dict:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(client, headers)
    await ingest(client, make_tick(datetime.now(TZ).replace(microsecond=0), household_id=household_id))
    response = await client.post("/assistant/me/ask", json={"question": "What is my solar output now?"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_enforce_replaces_an_answer_with_invented_numbers(client, invented_llm):
    _use_mode("enforce")
    body = await _ask_live(client, "ground-enforce@example.com")

    assert "9.9 kW" not in body["answer"]["observation"]
    assert "4,321" not in body["answer"]["estimated_impact"]
    assert body["meta"]["llm_used"] is False
    assert body["meta"]["fallback_reason"] == "ungrounded_numbers"
    assert body["meta"]["grounding"]["mode"] == "enforce"
    assert "9.9 kW" in body["meta"]["grounding"]["ungrounded"]
    assert body["meta"]["usage"]["total_tokens"] > 0  # the spent LLM call is still accounted for


async def test_observe_keeps_the_answer_but_reports_the_problem(client, invented_llm):
    _use_mode("observe")
    body = await _ask_live(client, "ground-observe@example.com")

    assert body["answer"]["observation"] == INVENTED.observation
    assert body["meta"]["llm_used"] is True
    assert body["meta"]["grounding"]["ungrounded"]


async def test_off_skips_the_check(client, invented_llm):
    _use_mode("off")
    body = await _ask_live(client, "ground-off@example.com")

    assert body["answer"]["observation"] == INVENTED.observation
    assert body["meta"]["grounding"] is None


async def test_a_grounded_answer_is_returned_as_written(client):
    fake = FakeStructuredLLM(
        draft=AnswerDraft(
            observation="Solar is producing 3.00 kW and the home is using 1.50 kW.",
            explanation="Solar covers the load.",
            recommendation="No action needed.",
            estimated_impact="No grid import expected.",
        )
    )
    app.dependency_overrides[get_structured_llm] = lambda: fake
    _use_mode("enforce")

    body = await _ask_live(client, "ground-ok@example.com")

    assert body["meta"]["llm_used"] is True
    assert body["meta"]["grounding"] == {"mode": "enforce", "checked": 2, "ungrounded": []}
