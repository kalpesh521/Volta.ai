"""
Per-question projection: each question sends only the fields it needs, nothing
derived is lost, and duplicated values are sent once.
"""
import copy
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.modules.assistant.deps import get_structured_llm
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.domain.projection import (
    Detail,
    project_analytics,
    project_facts,
    project_freshness,
)
from app.modules.assistant.graph.planner import build_plan
from app.modules.assistant.prompts import answer_guidance
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding

ASK = "/assistant/me/ask"

_SETUP_FIELDS = ('"panel_type"', '"inverter_brand"', '"discom"', '"avg_monthly_bill_inr"', '"meter_type"')

FACTS = {
    "household": {
        "system_type": "Hybrid",
        "location": "Pune, India",
        "panel_type": "Monocrystalline",
        "panel_qty": 10,
        "solar_capacity_kwp": 4.0,
        "inverter_brand": "Growatt",
        "battery_minimum_soc_percent": 20.0,
        "battery_backup_hours_target": 4,
        "discom": "MSEDCL",
        "tariff_rate": 8.5,
        "export_credit_inr_per_kwh": 6.2,
        "avg_monthly_bill_inr": 3500.0,
        "primary_goal": "maximize_self_consumption",
        "appliances": [
            {"name": "Refrigerator", "type": "refrigerator", "rated_power_kw": 0.15,
             "priority": "critical", "critical": True, "controllable": False},
        ],
    },
    "live_energy": {
        "timestamp": "2026-10-03T12:00:00+05:30",
        "data_source": "simulator",
        "data_quality": "simulated",
        "location": "Pune, India",
        "solar_kw": 3.0,
        "load_kw": 1.0,
        "solar_lifetime_kwh": 120.0,
        "warnings": ["energy_balance_error_kw=0.2 exceeds tolerance=0.05", "inverter derated"],
    },
    "battery": {"timestamp": "2026-10-03T12:00:00+05:30", "soc_percent": 62.0},
    "devices": {
        "timestamp": "2026-10-03T12:00:00+05:30",
        "devices": [
            {"device_id": "dev_ac", "name": "Air conditioner", "type": "air_conditioner", "state": "on",
             "current_power_kw": 1.2, "rated_power_kw": 1.5, "priority": "important",
             "critical": False, "controllable": True},
        ],
    },
    "weather": {
        "timestamp": "2026-10-03T12:00:00+05:30",
        "condition": "clear sky",
        "humidity_percent": 50.0,
        "sunset": "2026-10-03T18:12:00+05:30",
        "source": "open-meteo",
        "data_quality": "simulated",
    },
    "today_summary": {"timezone": "Asia/Kolkata", "reading_count": 12, "tariff_rate": 8.5,
                      "solar_generation_kwh": 4.2, "grid_export_kwh": 0.5},
    "recent_trend": {"samples": 12, "battery_soc_now_percent": 62.0, "battery_soc_change_percent": 6.0},
    "preferences": {"primary_goal": "maximize_self_consumption", "device_control_enabled": False},
}


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


def test_core_projection_keeps_core_numbers_and_drops_duplicates():
    original = copy.deepcopy(FACTS)
    out = project_facts(FACTS, set())

    assert FACTS == original
    household = out["household"]
    for key in ("system_type", "location", "solar_capacity_kwp", "battery_minimum_soc_percent",
                "tariff_rate", "primary_goal"):
        assert key in household, key
    for key in ("panel_type", "inverter_brand", "discom", "avg_monthly_bill_inr",
                "battery_backup_hours_target", "export_credit_inr_per_kwh", "appliances"):
        assert key not in household, key

    assert "preferences" not in out
    assert all("timestamp" not in section for section in out.values() if isinstance(section, dict))
    assert out["live_energy"] == {
        "data_quality": "simulated", "solar_kw": 3.0, "load_kw": 1.0, "warnings": ["inverter derated"],
    }
    assert out["devices"]["devices"] == [
        {"name": "Air conditioner", "state": "on", "current_power_kw": 1.2, "priority": "important"}
    ]
    assert out["weather"] == {"condition": "clear sky", "sunset": "18:12"}
    assert out["today_summary"] == {"solar_generation_kwh": 4.2, "grid_export_kwh": 0.5}
    assert out["recent_trend"] == {"battery_soc_change_percent": 6.0}


def test_detail_groups_opt_fields_back_in():
    out = project_facts(FACTS, set(Detail))

    assert out["household"]["panel_type"] == "Monocrystalline"
    assert out["household"]["avg_monthly_bill_inr"] == 3500.0
    assert out["household"]["appliances"] == [
        {"name": "Refrigerator", "rated_power_kw": 0.15, "priority": "critical"}
    ]
    assert out["live_energy"]["solar_lifetime_kwh"] == 120.0
    assert out["weather"]["humidity_percent"] == 50.0
    assert out["devices"]["devices"][0]["rated_power_kw"] == 1.5
    assert "type" not in out["devices"]["devices"][0]


def test_analytics_projection_drops_only_inputs_already_in_facts():
    facts = project_facts(FACTS, set())
    analytics = {
        "solar_surplus": {"solar_kw": 3.0, "load_kw": 9.9, "surplus_kw": 2.0, "state": "surplus"},
        "backup": {"battery_present": True, "soc_percent": 62.0, "reserve_soc_percent": 20.0,
                   "usable_energy_kwh": 4.2, "backup_hours": 4.2, "note": "n"},
        "anomalies": [{"code": "x", "severity": "warning", "message": "m"}],
        "appliance": {"appliance": "washing_machine", "device_name": "Washing machine",
                      "rating_source": "household_profile", "solar_surplus_kw": 2.0,
                      "expected_grid_import_kw": 0.0, "verdict": "run_now_on_solar"},
    }
    out = project_analytics(analytics, facts)

    assert out["solar_surplus"] == {"load_kw": 9.9, "surplus_kw": 2.0, "state": "surplus"}
    assert out["backup"] == {"usable_energy_kwh": 4.2, "backup_hours": 4.2, "note": "n"}
    assert out["anomalies"] == [{"severity": "warning", "message": "m"}]
    assert out["appliance"] == {
        "device_name": "Washing machine", "expected_grid_import_kw": 0.0, "verdict": "run_now_on_solar",
    }
    assert analytics["backup"]["soc_percent"] == 62.0


def test_freshness_projection_keeps_status_age_and_stale_message():
    full = {"status": "stale", "data_time": "2026-10-03T12:00:00+05:30", "age_seconds": 900,
            "threshold_seconds": 600, "message": "Live data is 15 minutes old."}
    assert project_freshness(full) == {
        "status": "stale", "age_seconds": 900, "message": "Live data is 15 minutes old.",
    }
    assert project_freshness({**full, "status": "fresh", "message": None}) == {
        "status": "fresh", "age_seconds": 900,
    }


def test_planner_declares_details_per_intent():
    assert build_plan([Intent.APPLIANCE_TIMING]).details == (Detail.BATTERY_LIMITS,)
    assert build_plan([Intent.LIVE_OVERVIEW]).details == ()
    assert set(build_plan([Intent.FULL_OVERVIEW]).details) == set(Detail)
    assert Detail.BILLING in build_plan([Intent.HOME_PROFILE]).details
    assert set(build_plan([Intent.GRID_IMPORT, Intent.WEATHER]).details) == {
        Detail.GRID_CONTRACT, Detail.WEATHER_DETAIL,
    }


def test_guidance_is_added_only_when_it_applies():
    assert answer_guidance(["grid_import"], '{"solar_kw":1}') == ""
    home = answer_guidance(["home_profile"], '{"sanctioned_load_kw":5}')
    assert "home_profile:" in home and "Informational only" in home
    assert "sanctioned_load_kw: grid connection limit" in home
    assert "full_overview:" not in home


async def test_appliance_prompt_skips_setup_fields_but_home_details_includes_them(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    headers = await auth_headers(client, "projection@example.com")
    household_id = await complete_hybrid_onboarding(client, headers)
    now = datetime.now(TZ).replace(microsecond=0)
    for minutes_ago in (10, 0):
        await ingest(client, make_tick(now - timedelta(minutes=minutes_ago), household_id=household_id))

    response = await client.post(ASK, json={"question": "Should I run the washing machine now?"}, headers=headers)
    assert response.status_code == 200, response.text
    appliance_prompt = fake_llm.last_prompt()
    assert '"verdict"' in appliance_prompt and '"battery_minimum_soc_percent"' in appliance_prompt
    for field in _SETUP_FIELDS:
        assert field not in appliance_prompt, field
    assert "home_profile:" not in appliance_prompt

    response = await client.post(ASK, json={"question": "What are my home details?"}, headers=headers)
    assert response.status_code == 200, response.text
    home_prompt = fake_llm.last_prompt()
    for field in _SETUP_FIELDS:
        assert field in home_prompt, field
    assert len(appliance_prompt) < 4000
