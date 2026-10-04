"""Shared fakes and telemetry builders for assistant tests."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from langchain_core.messages import BaseMessage

from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError
from app.ai.llm.gateway import StructuredResult, TokenUsage
from app.core.config import settings
from app.modules.assistant.domain.answers import AnswerDraft
from app.modules.assistant.domain.intents import Intent, IntentClassification

TZ = ZoneInfo("Asia/Kolkata")
INGEST_HEADERS = {"X-Ingest-Token": settings.INGEST_TOKEN}

DEFAULT_DRAFT = AnswerDraft(
    observation="LLM observation.",
    explanation="LLM explanation.",
    recommendation="LLM recommendation.",
    estimated_impact="LLM impact.",
)


class FakeStructuredLLM:
    """Records every call; returns canned structured outputs."""

    def __init__(
        self,
        *,
        available: bool = True,
        draft: AnswerDraft = DEFAULT_DRAFT,
        classification: IntentClassification | None = None,
        fail_answer: bool = False,
    ) -> None:
        self.available = available
        self.draft = draft
        self.classification = classification or IntentClassification(
            primary_intent=Intent.OUT_OF_SCOPE
        )
        self.fail_answer = fail_answer
        self.calls: list[tuple[LLMRole, type, list[BaseMessage]]] = []

    def is_available(self, role: LLMRole) -> bool:
        return self.available

    def roles_called(self) -> list[LLMRole]:
        return [role for role, _, _ in self.calls]

    def last_prompt(self) -> str:
        return "\n".join(str(m.content) for m in self.calls[-1][2])

    async def ainvoke(
        self,
        role: LLMRole,
        schema: type,
        messages: Sequence[BaseMessage],
        *,
        config: Any = None,
    ) -> StructuredResult:
        self.calls.append((role, schema, list(messages)))
        usage = TokenUsage(input_tokens=120, output_tokens=60, total_tokens=180)
        if schema is AnswerDraft:
            if self.fail_answer:
                raise LLMInvocationError("answer model call failed (TimeoutError)")
            return StructuredResult(output=self.draft, model="fake-answer", usage=usage, latency_ms=3)
        if schema is IntentClassification:
            return StructuredResult(
                output=self.classification, model="fake-router", usage=usage, latency_ms=2
            )
        raise AssertionError(f"unexpected schema {schema}")


def make_tick(
    ts: datetime,
    *,
    household_id: str,
    solar_kw: float = 3.0,
    load_kw: float = 1.5,
    charge_kw: float = 1.5,
    discharge_kw: float = 0.0,
    import_kw: float = 0.0,
    export_kw: float = 0.0,
    soc: float = 62.0,
    battery_status: str = "charging",
    grid_status: str = "available",
    devices: list[dict] | None = None,
    radiation: float = 700.0,
    cloud: float = 20.0,
) -> dict:
    interval_h = 1.0 / 60.0
    return {
        "timestamp": ts.isoformat(),
        "household_id": household_id,
        "data_source": "simulator",
        "data_quality": "simulated",
        "solar_power_kw": solar_kw,
        "solar_energy_interval_kwh": solar_kw * interval_h,
        "solar_energy_today_kwh": 4.2,
        "solar_energy_total_kwh": 120.0,
        "home_load_power_kw": load_kw,
        "home_consumption_interval_kwh": load_kw * interval_h,
        "home_consumption_today_kwh": 3.1,
        "home_consumption_total_kwh": 90.0,
        "battery_soc_percent": soc,
        "battery_soh_percent": 98.0,
        "battery_charge_power_kw": charge_kw,
        "battery_discharge_power_kw": discharge_kw,
        "battery_energy_available_kwh": 6.2,
        "battery_status": battery_status,
        "grid_status": grid_status,
        "grid_import_power_kw": import_kw,
        "grid_export_power_kw": export_kw,
        "total_import_kwh": 10.0,
        "total_export_kwh": 8.0,
        "solar_to_home_kw": min(solar_kw, load_kw),
        "solar_to_battery_kw": charge_kw,
        "solar_to_grid_kw": export_kw,
        "battery_to_home_kw": discharge_kw,
        "grid_to_home_kw": import_kw,
        "unserved_load_kw": 0.0,
        "devices": devices
        if devices is not None
        else [
            {
                "device_id": "dev_refrigerator",
                "device_name": "Refrigerator",
                "device_type": "refrigerator",
                "rated_power_kw": 0.15,
                "current_state": "on",
                "current_power_kw": 0.12,
                "energy_interval_kwh": 0.002,
                "critical": True,
                "controllable": False,
            },
            {
                "device_id": "dev_air_conditioner",
                "device_name": "Air conditioner",
                "device_type": "air_conditioner",
                "rated_power_kw": 1.5,
                "current_state": "on",
                "current_power_kw": 1.2,
                "energy_interval_kwh": 0.02,
                "critical": False,
                "controllable": True,
            },
        ],
        "weather": {
            "timestamp": ts.isoformat(),
            "temperature_c": 31.0,
            "humidity_percent": 50.0,
            "cloud_cover_percent": cloud,
            "precipitation_mm": 0.0,
            "precipitation_probability_percent": 5.0,
            "wind_speed_kmh": 8.0,
            "shortwave_radiation_wm2": radiation,
            "direct_radiation_wm2": radiation * 0.7,
            "diffuse_radiation_wm2": radiation * 0.3,
            "weather_code": 0,
            "source": "test",
            "data_quality": "simulated",
        },
    }


async def auth_headers(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/auth/signup",
        json={"name": "Assistant Tester", "email": email, "password": "StrongPass123"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": "StrongPass123"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def ingest(client: AsyncClient, tick: dict) -> None:
    response = await client.post("/energy/ingest", json=tick, headers=INGEST_HEADERS)
    assert response.status_code == 202, response.text
