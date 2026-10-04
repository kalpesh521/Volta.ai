"""
End-to-end assistant evals over HTTP.

Real onboarding, ingest, energy store and LangGraph workflow; only the LLM is
faked. Each eval pins which tools the graph selects and which guardrails fire.
"""
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app.ai.config import LLMRole
from app.modules.assistant.deps import get_structured_llm
from app.modules.assistant.domain.intents import Intent, IntentClassification
from main import app
from tests.assistant_helpers import TZ, FakeStructuredLLM, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding

ASK = "/assistant/me/ask"


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


async def _home(client: AsyncClient, email: str, **tick) -> tuple[dict[str, str], str, datetime | None]:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(
        client,
        headers,
        appliances=[
            {"appliance_key": "fridge", "is_critical": True},
            {"appliance_key": "ac", "is_critical": False},
            {"appliance_key": "heater", "is_critical": False},
        ],
    )
    ts = tick.pop("ts", datetime.now(TZ).replace(microsecond=0))
    if ts is not None:
        await ingest(client, make_tick(ts, household_id=household_id, **tick))
    return headers, household_id, ts


async def _ask(client: AsyncClient, headers: dict[str, str], question: str) -> dict:
    response = await client.post(ASK, json={"question": question}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_requires_jwt(client: AsyncClient, fake_llm: FakeStructuredLLM):
    response = await client.post(ASK, json={"question": "What is happening now?"})
    assert response.status_code == 401


async def test_rejects_blank_question(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers, _, _ = await _home(client, "assist-blank@example.com")
    response = await client.post(ASK, json={"question": "  "}, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_eval_battery_status_uses_battery_tool(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers, household_id, ts = await _home(client, "assist-battery@example.com")
    body = await _ask(client, headers, "What is my current battery status?")

    assert body["household_id"] == household_id
    assert body["intents"] == ["battery"]
    assert "get_battery_status" in body["tools_used"]
    assert "calculate_backup_duration" in body["analytics_used"]
    assert body["answer"]["observation"] == "LLM observation."
    assert body["answer"]["data_time"] == ts.isoformat()
    assert body["confidence"] == "high"
    assert body["meta"]["llm_used"] is True
    assert body["meta"]["classification_method"] == "rules"
    assert body["meta"]["usage"]["total_tokens"] == 180
    # Rules were confident → the router LLM was never paid for.
    assert fake_llm.roles_called() == [LLMRole.ANSWER]


async def test_eval_exporting_uses_live_and_daily(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers, _, _ = await _home(
        client,
        "assist-export@example.com",
        solar_kw=4.0,
        load_kw=1.0,
        charge_kw=0.0,
        export_kw=3.0,
        soc=98.0,
        battery_status="full",
    )
    body = await _ask(client, headers, "Why am I exporting power?")

    assert body["intents"] == ["grid_export"]
    assert {"get_live_energy_state", "get_daily_energy_summary"} <= set(body["tools_used"])
    assert {"calculate_solar_surplus", "calculate_solar_self_consumption"} <= set(body["analytics_used"])
    prompt = fake_llm.last_prompt()
    assert '"surplus_kw":3.0' in prompt
    assert '"state":"surplus"' in prompt


async def test_eval_geyser_uses_solar_battery_device_and_tariff(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    headers, _, _ = await _home(client, "assist-geyser@example.com")
    body = await _ask(client, headers, "Should I run the geyser?")

    assert body["intents"] == ["appliance_timing"]
    assert body["appliance"] == "water_heater"
    assert {
        "get_live_energy_state",
        "get_battery_status",
        "get_device_readings",
        "get_household_profile",
    } <= set(body["tools_used"])
    assert "evaluate_appliance_run" in body["analytics_used"]
    prompt = fake_llm.last_prompt()
    assert '"tariff_rate":8.5' in prompt
    assert '"verdict":' in prompt


async def test_eval_stale_data_warns_instead_of_confident_advice(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    old = datetime.now(TZ).replace(microsecond=0) - timedelta(hours=3)
    headers, _, _ = await _home(client, "assist-stale@example.com", ts=old)
    body = await _ask(client, headers, "Should I run the washing machine now?")

    assert body["data_freshness"]["status"] == "stale"
    assert body["confidence"] == "low"
    assert any("old" in w for w in body["warnings"])
    assert body["answer"]["recommendation"].startswith("Because this data is not current")
    assert '"status":"stale"' in fake_llm.last_prompt()


async def test_device_control_is_refused_without_tools_or_llm(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    headers, _, _ = await _home(client, "assist-control@example.com")
    body = await _ask(client, headers, "Turn off the AC")

    assert body["intents"] == ["device_control"]
    assert body["tools_used"] == []
    assert body["meta"]["fallback_reason"] == "device_control"
    assert "cannot control devices" in body["answer"]["observation"]
    assert fake_llm.calls == []


async def test_no_telemetry_returns_grounded_no_data_answer(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    headers, _, _ = await _home(client, "assist-empty@example.com", ts=None)
    body = await _ask(client, headers, "What is happening in my home right now?")

    assert body["data_freshness"]["status"] == "missing"
    assert body["meta"]["fallback_reason"] == "no_data"
    assert body["confidence"] == "low"
    assert body["answer"]["data_time"] == "unavailable"
    assert fake_llm.calls == []


async def test_without_llm_key_answers_deterministically(client: AsyncClient, fake_llm: FakeStructuredLLM):
    fake_llm.available = False
    headers, _, _ = await _home(client, "assist-nokey@example.com")
    body = await _ask(client, headers, "What is happening in my home right now?")

    assert body["meta"]["llm_used"] is False
    assert body["meta"]["fallback_reason"] == "llm_unavailable"
    assert "Solar is producing 3.00 kW" in body["answer"]["observation"]
    assert fake_llm.calls == []


async def test_llm_failure_degrades_to_deterministic_answer(
    client: AsyncClient, fake_llm: FakeStructuredLLM
):
    fake_llm.fail_answer = True
    headers, _, _ = await _home(client, "assist-llmfail@example.com", solar_kw=0.5, load_kw=2.0, charge_kw=0.0, import_kw=1.5)
    body = await _ask(client, headers, "Why am I importing electricity?")

    assert body["meta"]["llm_used"] is False
    assert body["meta"]["fallback_reason"] == "llm_error"
    assert "importing 1.50 kW" in body["answer"]["observation"]
    assert "midday" in body["answer"]["recommendation"]


async def test_ambiguous_question_uses_router_llm(client: AsyncClient, fake_llm: FakeStructuredLLM):
    fake_llm.classification = IntentClassification(
        primary_intent=Intent.LIVE_OVERVIEW, secondary_intents=[Intent.SAVINGS]
    )
    headers, _, _ = await _home(client, "assist-router@example.com")
    body = await _ask(client, headers, "Tell me about my energy")

    assert fake_llm.roles_called() == [LLMRole.ROUTER, LLMRole.ANSWER]
    assert body["meta"]["classification_method"] == "llm"
    assert body["intents"] == ["live_overview", "savings"]
    assert "get_daily_energy_summary" in body["tools_used"]


async def test_prompt_is_compact_and_grounded(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers, _, _ = await _home(client, "assist-compact@example.com")
    await _ask(client, headers, "What is happening in my home right now?")
    prompt = fake_llm.last_prompt()

    assert "<facts>" in prompt and "<analytics>" in prompt
    assert "read-only" in prompt and "You cannot control devices" in prompt
    for raw_field in (
        "total_import_kwh",
        "solar_energy_total_kwh",
        "household_id\":\"home_",
        '"preferences"',
        '"timestamp"',
        '"device_id"',
        "threshold_seconds",
    ):
        assert raw_field not in prompt


async def test_household_route_enforces_ownership(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers_a, home_a, _ = await _home(client, "assist-owner-a@example.com")
    headers_b, _, _ = await _home(client, "assist-owner-b@example.com")

    own = await client.post(
        f"/assistant/{home_a}/ask", json={"question": "Why am I exporting power?"}, headers=headers_a
    )
    assert own.status_code == 200
    assert own.json()["household_id"] == home_a

    leaked = await client.post(
        f"/assistant/{home_a}/ask", json={"question": "Why am I exporting power?"}, headers=headers_b
    )
    assert leaked.status_code == 404


async def test_status_reports_mode(client: AsyncClient, fake_llm: FakeStructuredLLM):
    headers = await auth_headers(client, "assist-status@example.com")
    response = await client.get("/assistant/status", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "llm"
    assert body["answer_model"]["available"] is True
    assert "api_key" not in response.text.lower()
