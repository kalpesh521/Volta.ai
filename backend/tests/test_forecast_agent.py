"""The Forecast agent: adapter over the engine. The engine has its own tests; this pins the wiring and the honesty."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agents_platform.agents.forecast import ForecastWeatherError, WeatherSeries
from app.agents_platform.agents.forecast.agent import (
    NEEDS_LOCATION_REASON,
    ForecastAgent,
    history_points,
    horizon_for,
)
from app.agents_platform.agents.planning_data import AuthorizedData
from app.agents_platform.contracts import AgentCall, AgentName, AgentStatus
from app.agents_platform.guardrails.policy import DataForbiddenError
from app.agents_platform.orchestration.retry import TransientAgentError
from app.modules.assistant.domain.grounding import check_grounding
from app.modules.energy.schemas import HomeLocationOut

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 14, 9, 30, tzinfo=IST)
PUNE = HomeLocationOut(name="Pune", latitude=18.52, longitude=73.86, timezone="Asia/Kolkata")


def live(location=PUNE, *, age_minutes=0, cloud=20.0):
    return SimpleNamespace(
        location=location,
        timestamp=NOW - timedelta(minutes=age_minutes),
        weather=SimpleNamespace(cloud_cover_percent=cloud) if cloud is not None else None,
    )


def record(ts: datetime, solar: float, load: float):
    return SimpleNamespace(timestamp=ts, solar_power_kw=solar, home_load_power_kw=load)


def day_of_history(days: int = 3):
    out = []
    for d in range(days, 0, -1):
        for h in range(24):
            ts = (NOW - timedelta(days=d)).replace(hour=h, minute=0)
            solar = max(0.0, 3.5 * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0
            out.append(record(ts, solar, 0.6 + 0.8 * (18 <= h <= 22)))
    return out


class FakeData:
    def __init__(self, *, live_value=None, records=None, error: Exception | None = None):
        self._live = live() if live_value is None else live_value
        self._records = day_of_history() if records is None else records
        self._error = error
        self.reads: list[str] = []

    async def live(self):
        self.reads.append("live")
        if self._error:
            raise self._error
        return None if self._live is False else self._live

    async def history(self, limit):
        self.reads.append("history")
        return self._records

    def profile(self):
        self.reads.append("profile")
        return SimpleNamespace(solar_capacity_kwp=4.0, inverter_capacity_kw=5.0)

    def onboarding(self):
        return SimpleNamespace()


def call(question="How much solar will I generate tomorrow?") -> AgentCall:
    return AgentCall(agent=AgentName.FORECAST, question=question, intents=("forecast",), deadline_seconds=5)


def agent(data=None, **kw) -> ForecastAgent:
    return ForecastAgent(data or FakeData(), now=lambda: NOW, stale_threshold_seconds=600, **kw)


# --- the agent ------------------------------------------------------------------------------------


async def test_a_forecast_comes_back_as_facts_with_an_honest_confidence_and_freshness():
    result = await agent().run(call())
    facts = result.facts["forecast"]

    assert result.status is AgentStatus.OK and result.agent is AgentName.FORECAST
    assert facts["solar_kwh_next_24h"] > 0 and facts["peak_solar"]["kw"] > 0
    assert "methods" in facts and "confidence" in facts and facts["hourly"]
    assert result.confidence == facts["confidence"]["level"]
    assert result.freshness["status"] == "fresh" and result.requires_telemetry and result.live_advice
    assert "solar_forecast" in result.analytics_used and "load_forecast" in result.analytics_used


async def test_old_readings_make_the_result_say_so():
    result = await agent(FakeData(live_value=live(age_minutes=45))).run(call())
    assert result.freshness["status"] == "stale"


async def test_without_history_it_still_forecasts_and_says_the_load_is_unknown():
    result = await agent(FakeData(records=[])).run(call())
    facts = result.facts["forecast"]
    assert result.status is AgentStatus.OK and facts["methods"]["load"] == "unknown"
    assert "load_forecast" not in result.analytics_used and facts["confidence"]["level"] != "high"


async def test_a_home_without_coordinates_is_told_what_is_missing_not_given_a_forecast_for_the_wrong_sun():
    for location in (None, HomeLocationOut(name="Somewhere"), HomeLocationOut(latitude=18.5)):
        result = await agent(FakeData(live_value=live(location))).run(call())
        assert result.status is AgentStatus.PARTIAL and result.fallback_reason == NEEDS_LOCATION_REASON
        assert result.answers_itself and result.facts == {} and "location" in result.draft.observation


async def test_a_home_with_no_telemetry_at_all_is_told_so():
    result = await agent(FakeData(live_value=False)).run(call())
    assert result.fallback_reason == NEEDS_LOCATION_REASON and "No energy data" in result.draft.observation


async def test_a_flaky_energy_store_is_a_transient_error_so_the_node_retries_it():
    for error in (ConnectionError("reset"), TimeoutError(), OSError("down")):
        with pytest.raises(TransientAgentError):
            await agent(FakeData(error=error)).run(call())


async def test_a_bug_is_not_dressed_up_as_transient():
    with pytest.raises(RuntimeError):
        await agent(FakeData(error=RuntimeError("bug"))).run(call())


async def test_the_weather_source_is_used_and_its_failure_degrades_to_a_warning_not_an_error():
    class Broken:
        async def hourly(self, *, latitude, longitude, times):
            raise ForecastWeatherError("503")

    result = await agent(weather=lambda: Broken()).run(call())
    assert result.status is AgentStatus.OK
    assert result.warnings, "the user must be told the weather source failed"

    class Clear:
        calls = 0

        async def hourly(self, *, latitude, longitude, times):
            Clear.calls += 1
            from app.agents_platform.agents.forecast import WeatherHour

            return WeatherSeries("stub", "model", tuple(WeatherHour(t, 0.0) for t in times))

    clear = await agent(weather=lambda: Clear()).run(call())
    assert Clear.calls == 1
    cloudy = await agent().run(call())
    assert clear.facts["forecast"]["solar_kwh_next_24h"] >= cloudy.facts["forecast"]["solar_kwh_next_24h"]


async def test_the_draft_the_model_may_fall_back_to_is_grounded_in_the_facts():
    result = await agent().run(call())
    grounding = check_grounding(result.draft.model_dump(), facts=[result.facts])
    assert grounding.ok, grounding.ungrounded


async def test_the_agent_reads_only_what_its_job_needs_and_nothing_about_other_homes():
    data = FakeData()
    result = await agent(AuthorizedData(AgentName.FORECAST, data)).run(call())
    assert result.status is AgentStatus.OK and set(data.reads) == {"live", "history", "profile"}
    with pytest.raises(DataForbiddenError):  # the bill agent has no scope for live readings
        await AuthorizedData(AgentName.BILL_TARIFF, data).live()


# --- helpers --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question, hours",
    [
        ("How much solar will I generate tomorrow?", 48),
        ("Forecast my solar for the next 36 hours", 36),
        ("forecast the next 99 hours", 48),
        ("Will I have enough solar this afternoon?", 24),
        ("next 1 hour", 1),
    ],
)
def test_the_horizon_follows_the_question_and_is_capped(question, hours):
    assert horizon_for(question) == hours


def test_telemetry_is_mapped_to_history_points_field_for_field():
    ts = NOW
    points = history_points([record(ts, 2.5, 1.2)])
    assert points[0].timestamp == ts and points[0].solar_kw == 2.5 and points[0].load_kw == 1.2
    assert history_points([]) == ()
