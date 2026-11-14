"""
Forecast, optimiser and bill agents over HTTP: real onboarding, ingest, energy store, supervisor and LangGraph.
Only the answer model is faked. This is the test that proves the planning agents are wired into the product, with
their plan-tier rules, their honesty flags and the explain-and-do offer.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.agents_platform.agents.device_action.deps import get_command_publisher
from app.agents_platform.agents.device_action.publisher import RecordingPublisher
from app.agents_platform.api.deps import get_platform
from app.agents_platform.platform import build_platform
from app.ai.config import AISettings, LLMRole
from app.core.config import settings
from app.models.user import User
from app.modules.assistant.deps import get_structured_llm
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_supervisor_api import ask_v2

LOCATION = {"name": "Pune", "latitude": 18.52, "longitude": 73.86, "timezone": "Asia/Kolkata"}


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    app.dependency_overrides[get_platform] = lambda: build_platform(AISettings(_env_file=None))
    return fake


@pytest.fixture
def publisher(monkeypatch) -> RecordingPublisher:
    recorder = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: recorder
    monkeypatch.setattr(settings, "DEVICE_CONTROL_ENABLED", True)
    monkeypatch.setattr(settings, "DEVICE_KILL_SWITCH", False)
    return recorder


async def planning_home(client: AsyncClient, db_session, email: str, *, tier: str = "free"):
    """A home with a few days of readings that carry the home's location, like the simulator's do."""
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(
        client,
        headers,
        appliances=[
            {"appliance_key": "fridge", "is_critical": True},
            {"appliance_key": "wash", "is_critical": False},
            {"appliance_key": "heater", "is_critical": False},
        ],
    )
    if tier != "free":
        user = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
        user.plan_tier = tier
        await db_session.commit()
    now = datetime.now(TZ).replace(microsecond=0)
    await ingest(client, {**make_tick(now, household_id=household_id), "location": LOCATION})
    return headers


async def test_a_forecast_question_runs_the_forecast_agent_and_the_model_sees_its_facts(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-forecast@example.com")

    body = await ask_v2(client, headers, "How much solar will I generate tomorrow?")

    assert body["intents"] == ["forecast"] and body["route"] == "single"
    assert [a["agent"] for a in body["agents"]] == ["forecast"] and body["agents"][0]["status"] == "ok"
    assert fake_llm.roles_called() == [LLMRole.ANSWER]
    prompt = fake_llm.last_prompt()
    assert '"peak_solar"' in prompt and "forecast:" in prompt, "the forecast facts and its answer guide reach the model"
    assert body["meta"]["classification_method"] == "rules"


async def test_an_optimiser_question_from_a_free_user_is_refused_with_fixed_wording_and_costs_no_tokens(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-optimize-free@example.com", tier="free")

    body = await ask_v2(client, headers, "Optimize my battery usage")

    assert body["agents"][0]["agent"] == "optimizer" and body["agents"][0]["status"] == "refused"
    assert "Plus plan" in body["answer"]["observation"] and body["meta"]["fallback_reason"] == "plan_required"
    assert fake_llm.calls == [] and body["confidence"] == "high"


async def test_the_same_optimiser_question_from_a_plus_user_produces_a_plan(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-optimize-plus@example.com", tier="plus")

    body = await ask_v2(client, headers, "Optimize my battery usage")

    assert body["agents"][0]["status"] == "ok" and fake_llm.roles_called() == [LLMRole.ANSWER]
    prompt = fake_llm.last_prompt()
    assert '"plan_summary"' in prompt and "optimize:" in prompt
    assert any("UNVERIFIED TARIFF" in w for w in body["warnings"]), "rupee figures must carry the tariff caveat"
    assert body["confidence"] in ("medium", "low")


async def test_a_bill_estimate_is_free_and_says_the_tariff_is_not_official(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-bill-free@example.com", tier="free")

    body = await ask_v2(client, headers, "Estimate my bill next month")

    assert body["intents"] == ["bill_estimate"] and body["agents"][0]["agent"] == "bill_tariff"
    assert body["agents"][0]["status"] == "ok"
    assert any("UNVERIFIED TARIFF" in w for w in body["warnings"])
    prompt = fake_llm.last_prompt()
    assert '"tariff_verified":false' in prompt and "bill_estimate:" in prompt and '"total_inr"' in prompt


async def test_a_what_if_for_a_free_user_gets_the_estimate_and_is_told_the_scenario_needs_plus(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-whatif-free@example.com", tier="free")

    body = await ask_v2(client, headers, "What if I add 2 kWp of solar, how much would my bill drop?")

    assert any("Plus plan" in w for w in body["warnings"])
    assert '"what_if"' not in fake_llm.last_prompt()


async def test_a_what_if_for_a_plus_user_runs_the_scenario_with_its_assumptions(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-whatif-plus@example.com", tier="plus")

    body = await ask_v2(client, headers, "What if I add 2 kWp of solar, how much would my bill drop?")

    prompt = fake_llm.last_prompt()
    assert '"what_if"' in prompt and "assumed_solar_yield_kwh_per_kwp_month" in prompt
    assert body["agents"][0]["status"] == "ok"


async def test_a_two_part_question_runs_a_planning_agent_beside_energy_insight_in_parallel(client, db_session, fake_llm):
    headers = await planning_home(client, db_session, "plan-multi@example.com", tier="plus")

    body = await ask_v2(client, headers, "Forecast my solar for tomorrow and also why is the battery discharging?")

    assert body["route"] == "multi" and {a["agent"] for a in body["agents"]} == {"forecast", "energy_insight"}
    assert fake_llm.roles_called() == [LLMRole.ANSWER], "one answer call for the whole compound question"


async def test_explain_and_do_answers_the_question_and_offers_the_command_without_sending_anything(
    client, db_session, fake_llm, publisher
):
    headers = await planning_home(client, db_session, "plan-explain-do@example.com")

    body = await ask_v2(client, headers, "Why is the battery draining so fast, and turn off the AC")

    assert [a["agent"] for a in body["agents"]] == ["energy_insight"] and body["proposal"] is None
    assert body["answer"]["recommendation"].endswith(
        'send "turn off the AC" on its own and I will ask you to confirm it first.'
    )
    assert publisher.commands == [], "nothing may be queued from a combined message"


async def test_the_offered_command_still_goes_through_confirm_before_act_when_sent_on_its_own(
    client, db_session, fake_llm, publisher
):
    headers = await planning_home(client, db_session, "plan-offer-followup@example.com")

    body = await ask_v2(client, headers, "Turn off the AC")

    assert body["agents"][0]["agent"] == "device_action" and publisher.commands == []  # proposed, awaiting confirmation


async def test_a_home_whose_readings_carry_no_location_is_told_what_is_missing(client, db_session, fake_llm):
    headers = await auth_headers(client, "plan-noloc@example.com")
    household_id = await complete_hybrid_onboarding(client, headers)
    await ingest(client, make_tick(datetime.now(TZ).replace(microsecond=0), household_id=household_id))

    body = await ask_v2(client, headers, "How much solar will I generate tomorrow?")

    assert body["agents"][0]["status"] == "partial" and "location" in body["answer"]["observation"]
    assert fake_llm.calls == [], "a deterministic answer needs no model"
