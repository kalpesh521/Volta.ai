"""
Forecast agent (L0, read-only): hourly solar and home-load forecast with honest intervals.

A thin adapter. The engine (`engine.py`) is a pure function; this layer reads the home's data through the
authorised data port, maps it to the engine's inputs, picks the weather source, and hands the result to the
synthesizer as facts. It never writes the answer and never asks a model for a number.

What it will not do: invent a place. Solar physics needs coordinates, which the telemetry carries (the home's
location block). Without them it says what is missing instead of forecasting for the wrong sun.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime

from langchain_core.runnables import RunnableConfig

from app.agents_platform.agents.forecast.engine import forecast
from app.agents_platform.agents.forecast.models import (
    DEFAULT_HORIZON_HOURS,
    MAX_HORIZON_HOURS,
    ForecastInputs,
    HistoryPoint,
    WeatherForecastPort,
)
from app.agents_platform.agents.forecast.weather import PersistenceWeather
from app.agents_platform.agents.planning_data import PlanningData
from app.agents_platform.contracts import AgentCall, AgentName, AgentResult, AgentStatus, AnswerParts
from app.agents_platform.orchestration.retry import TransientAgentError
from app.modules.assistant.domain.freshness import assess_freshness
from app.modules.energy.schemas import TelemetryRecord

NEEDS_LOCATION_REASON = "location_missing"
HISTORY_RECORDS = 2000  # the most the energy service returns: about a day and a half at one reading a minute
_HOURS = re.compile(r"\bnext\s+(\d{1,2})\s+hours?\b")
_TRANSIENT = (OSError, TimeoutError, ConnectionError)  # a store or network that may answer next time

WeatherFactory = Callable[[], WeatherForecastPort | None]


def horizon_for(question: str) -> int:
    """24 hours by default, more when the question reaches past tonight ("tomorrow", "next 36 hours")."""
    text = question.lower()
    if match := _HOURS.search(text):
        return max(1, min(MAX_HORIZON_HOURS, int(match.group(1))))
    return MAX_HORIZON_HOURS if "tomorrow" in text else DEFAULT_HORIZON_HOURS


def history_points(records: list[TelemetryRecord]) -> tuple[HistoryPoint, ...]:
    return tuple(HistoryPoint(r.timestamp, r.solar_power_kw, r.home_load_power_kw) for r in records)


class ForecastAgent:
    name = AgentName.FORECAST

    def __init__(
        self,
        data: PlanningData,
        *,
        now: Callable[[], datetime],
        stale_threshold_seconds: int,
        weather: WeatherFactory = lambda: None,
    ) -> None:
        self._data = data
        self._now = now
        self._stale = stale_threshold_seconds
        self._weather = weather

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult:
        started = time.perf_counter()
        try:
            live = await self._data.live()
            records = await self._data.history(HISTORY_RECORDS) if live is not None else []
        except _TRANSIENT as exc:
            raise TransientAgentError(f"energy data unavailable: {type(exc).__name__}") from exc
        profile = self._data.profile()
        now = self._now()

        location = live.location if live is not None else None
        if live is None or location is None or location.latitude is None or location.longitude is None:
            return self._needs(started, live is None)

        observed = live.weather
        inputs = ForecastInputs(
            latitude=location.latitude,
            longitude=location.longitude,
            timezone=location.timezone or "UTC",
            solar_capacity_kwp=profile.solar_capacity_kwp,
            inverter_capacity_kw=profile.inverter_capacity_kw,
            history=history_points(records),
            horizon_hours=horizon_for(call.question),
            observed_cloud_cover_pct=observed.cloud_cover_percent if observed else None,
            observed_at=live.timestamp if observed else None,
        )
        port = self._weather() or PersistenceWeather(
            observed_cloud_pct=inputs.observed_cloud_cover_pct, observed_at=inputs.observed_at
        )
        result = await forecast(inputs, port, now)
        facts = result.to_facts()
        freshness = assess_freshness(live.timestamp, now=now, threshold_seconds=self._stale)

        return AgentResult(
            agent=self.name,
            status=AgentStatus.OK,
            facts={"forecast": facts},
            freshness=freshness.model_dump(mode="json"),
            analytics_used=["solar_forecast", *(["load_forecast"] if result.methods.load != "unknown" else [])],
            draft=_draft(facts),
            warnings=list(result.warnings),
            confidence=result.confidence.level,
            live_advice=True,
            requires_telemetry=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _needs(self, started: float, no_telemetry: bool) -> AgentResult:
        why = (
            "No energy data has been received for this home yet."
            if no_telemetry
            else "Your home's location is not in the energy data, so I cannot work out where the sun will be."
        )
        return AgentResult(
            agent=self.name,
            status=AgentStatus.PARTIAL,
            draft=AnswerParts(
                observation=why,
                explanation="A solar forecast needs the home's position and recent readings, so I did not make one up.",
                recommendation="Check that your home's location is set and that the system is sending data, then ask again.",
                estimated_impact="Not enough data to estimate.",
            ),
            fallback_reason=NEEDS_LOCATION_REASON,
            confidence="low",
            live_advice=False,
            requires_telemetry=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _draft(facts: dict) -> AnswerParts:
    """Plain wording from the facts, used only if the model is switched off."""
    peak = facts.get("peak_solar") or {}
    solar = facts.get("solar_kwh_next_24h")
    observation = (
        f"Expected solar over the next 24 hours is about {solar} kWh."
        if solar is not None
        else "A solar forecast is available for the next hours."
    )
    explanation = (
        f"Output should peak around {str(peak.get('hour_start', ''))[11:16]} at about {peak.get('kw')} kW."
        if peak
        else "The forecast comes from the sun's position and the expected cloud cover."
    )
    surplus = facts.get("surplus_window")
    recommendation = (
        f"Run heavy appliances in the surplus window, {str(surplus['start'])[11:16]} to {str(surplus['end'])[11:16]}."
        if isinstance(surplus, dict) and surplus.get("start")
        else "Plan heavier loads for the middle of the day when solar is strongest."
    )
    return AnswerParts(
        observation=observation,
        explanation=explanation,
        recommendation=recommendation,
        estimated_impact="A forecast is an estimate, not a guarantee.",
    )
