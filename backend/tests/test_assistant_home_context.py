"""
Home-profile and full-overview coverage: every onboarding field and simulator
reading reaches the model, identity data never does, and the facts budget holds.
"""
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.modules.assistant.deps import get_structured_llm
from app.modules.assistant.domain.context import fit_to_budget, trend_facts
from app.modules.assistant.domain.intents import Intent, classify_by_rules
from app.modules.assistant.graph.planner import build_plan
from app.modules.assistant.tools.registry import ToolName
from app.modules.energy.schemas import TelemetryRecord
from app.modules.energy.service import to_battery
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding

ASK = "/assistant/me/ask"


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


async def _onboarded(client: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(
        client,
        headers,
        appliances=[
            {"appliance_key": "fridge", "is_critical": True},
            {"appliance_key": "wash", "is_critical": False},
        ],
    )
    return headers, household_id


async def _ask(client: AsyncClient, headers: dict[str, str], question: str) -> dict:
    response = await client.post(ASK, json={"question": question}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "question",
    [
        "What are my home details?",
        "Tell me the home details of the user",
        "What is my system setup?",
        "Show my household profile",
        "How many panels do I have?",
        "What inverter do I have?",
        "What is my sanctioned load?",
        "Which DISCOM am I with?",
        "What was my average monthly bill?",
        "Where is my home?",
        "What is my battery capacity?",
    ],
)
def test_home_setup_questions_route_to_home_profile(question):
    match = classify_by_rules(question)
    assert match.primary is Intent.HOME_PROFILE
    assert match.confident is True


@pytest.mark.parametrize(
    "question",
    ["Tell me everything about my home", "Give me a full report", "Tell me about my home"],
)
def test_everything_questions_route_to_full_overview(question):
    assert classify_by_rules(question).intents == (Intent.FULL_OVERVIEW,)


def test_home_profile_does_not_need_telemetry_but_full_overview_loads_every_tool():
    profile = build_plan([Intent.HOME_PROFILE])
    assert profile.requires_telemetry is False
    assert profile.live_advice is False
    assert ToolName.HOUSEHOLD_PROFILE in profile.tools

    overview = build_plan([Intent.FULL_OVERVIEW])
    assert set(overview.tools) == set(ToolName)
    assert overview.live_advice is True

    assert build_plan([Intent.HOME_PROFILE, Intent.BATTERY]).requires_telemetry is False
    assert build_plan([Intent.BATTERY, Intent.HOME_PROFILE]).requires_telemetry is True


def test_simulator_fault_code_zero_means_no_fault():
    base = make_tick(datetime(2026, 10, 3, 12, 0, tzinfo=TZ), household_id="home_fault")
    healthy = TelemetryRecord.model_validate({**base, "battery_fault_code": 0})
    faulty = TelemetryRecord.model_validate({**base, "battery_fault_code": 7})
    assert to_battery(healthy).fault_code is None
    assert to_battery(faulty).fault_code == "7"


def test_trend_summarises_recent_window():
    start = datetime(2026, 10, 3, 10, 0, tzinfo=TZ)
    records = [
        TelemetryRecord.model_validate(
            make_tick(
                start + timedelta(minutes=10 * i),
                household_id="home_trend",
                solar_kw=1.0 + i,
                load_kw=1.0,
                charge_kw=float(i),
                soc=50.0 + 2 * i,
            )
        )
        for i in range(4)
    ]
    trend = trend_facts(list(reversed(records)), window_minutes=60)

    assert trend is not None
    assert trend.samples == 4
    assert trend.window_minutes == 30
    assert trend.solar_kw_start == 1.0 and trend.solar_kw_now == 4.0
    assert trend.solar_direction == "rising"
    assert trend.battery_soc_change_percent == 6.0
    assert trend_facts(records[:1]) is None


def test_fit_to_budget_drops_detail_before_core_numbers():
    payload = {
        "household": {
            "tariff_rate": 8.5,
            "appliances": [
                {"name": f"A{i}", "type": "x", "rated_power_kw": 1.0, "priority": "flexible",
                 "critical": i == 0, "controllable": True}
                for i in range(10)
            ],
        },
        "live_energy": {"solar_kw": 3.0, "load_kw": 1.5, "warnings": ["w"] * 5},
        "hourly_summary": {"peak_solar_hour": "12:00", "hours": [{"hour": f"{h:02d}:00"} for h in range(24)]},
        "weather": {"cloud_cover_percent": 20, "humidity_percent": 50, "wind_speed_kmh": 8},
    }
    fitted, reductions = fit_to_budget(payload, max_chars=400)

    assert reductions[0] == "hourly_detail"
    assert "hours" not in fitted["hourly_summary"]
    assert fitted["hourly_summary"]["peak_solar_hour"] == "12:00"
    assert fitted["live_energy"]["solar_kw"] == 3.0
    assert fitted["household"]["tariff_rate"] == 8.5
    assert "hours" in payload["hourly_summary"]

    untouched, none = fit_to_budget(payload, max_chars=100_000)
    assert none == [] and untouched == payload


async def test_home_details_answer_without_any_telemetry(client: AsyncClient, fake_llm: FakeStructuredLLM):
    fake_llm.available = False
    headers, _ = await _onboarded(client, "home-profile-empty@example.com")
    body = await _ask(client, headers, "What are my home details?")

    assert body["intents"][0] == "home_profile"
    assert body["meta"]["fallback_reason"] == "llm_unavailable"
    assert body["confidence"] == "high"
    assert body["warnings"] == []
    text = " ".join(body["answer"].values())
    for expected in (
        "Hybrid",
        "Pune, India",
        "10 Monocrystalline panels",
        "Growatt",
        "10.0 kWh",
        "20% reserve",
        "target backup 4 hours",
        "MSEDCL",
        "sanctioned load 5.0 kW",
        "₹3500",
        "Refrigerator (critical)",
    ):
        assert expected in text, expected


async def test_model_sees_all_onboarding_and_simulator_fields_but_no_identity(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    email = "home-profile-full@example.com"
    headers, household_id = await _onboarded(client, email)
    now = datetime.now(TZ).replace(microsecond=0)
    for minutes_ago in (30, 15, 0):
        tick = make_tick(now - timedelta(minutes=minutes_ago), household_id=household_id)
        tick["battery_fault_code"] = 0
        await ingest(client, tick)

    body = await _ask(client, headers, "Tell me everything about my home")
    prompt = fake_llm.last_prompt()

    assert body["intents"] == ["full_overview"]
    assert {c["name"] for c in body["meta"]["tool_calls"]} == {t.value for t in ToolName}
    assert "get_recent_trend" in body["tools_used"]
    for field in (
        '"panel_qty":10',
        '"panel_type":"Monocrystalline"',
        '"inverter_brand":"Growatt"',
        '"avg_monthly_bill_inr":3500',
        '"battery_backup_hours_target":4',
        '"sanctioned_load_kw":5',
        '"discom":"MSEDCL"',
        '"solar_lifetime_kwh":120',
        '"lifetime_import_kwh":10',
        '"condition":"clear sky"',
        '"humidity_percent":50',
        '"recent_trend":',
    ):
        assert field in prompt, field
    for secret in (email, "Assistant Tester", household_id, "fault_code"):
        assert secret not in prompt, secret
    assert not any("fault" in w for w in body["warnings"])
