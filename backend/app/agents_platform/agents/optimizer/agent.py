"""
Optimizer agent (L0, read-only): the best battery strategy and appliance schedule for the next day.

It joins three engines that already exist and adds no arithmetic of its own: the forecast (what the sun and the
house will do), the tariff (what a kWh costs in each hour) and the schedule solver. Everything the answer may quote
comes out of the solver's own `to_facts()`, and the solver checks its plan with an independent verifier, so a plan
with a violated constraint never reaches the user.

It only advises. It never changes a device: acting on a suggestion is a separate request that goes through the
Device Action agent and the user's confirmation.

Assumptions it has to make are stated in the facts, not hidden: a sanctioned load when onboarding has none, a typical
duration for each appliance, that the forecast is certain. Prices come from the tariff in force and carry its
`verified` flag, which is false for the shipped illustrative tables.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from langchain_core.runnables import RunnableConfig

from app.agents_platform.agents.bill_tariff.tables import TariffNotFound, table_for
from app.agents_platform.agents.bill_tariff.usage import usage_from_bill
from app.agents_platform.agents.forecast.agent import HISTORY_RECORDS, history_points
from app.agents_platform.agents.forecast.engine import forecast
from app.agents_platform.agents.forecast.models import ForecastInputs, WeatherForecastPort
from app.agents_platform.agents.forecast.weather import PersistenceWeather
from app.agents_platform.agents.optimizer import (
    ApplianceSpec,
    BatterySpec,
    Goal,
    GridSpec,
    OptimizationProblem,
    SlotInput,
    Status,
    optimize,
)
from app.agents_platform.agents.optimizer.pricing import band_at, export_credit, marginal_prices
from app.agents_platform.agents.planning_data import PlanningData
from app.agents_platform.contracts import AgentCall, AgentName, AgentResult, AgentStatus, AnswerParts
from app.agents_platform.orchestration.retry import TransientAgentError
from app.modules.assistant.domain.freshness import assess_freshness
from app.modules.assistant.domain.intents import ApplianceType, detect_appliance

NEEDS_INPUT_REASON = "optimizer_input_missing"
CATEGORY = "residential"
HORIZON_HOURS = 24
DEFAULT_SANCTIONED_LOAD_KW = 5.0
_TRANSIENT = (OSError, TimeoutError, ConnectionError)

# Typical run length in whole hours for a household appliance. A stated assumption, put in the facts.
APPLIANCE_HOURS: dict[ApplianceType, int] = {
    ApplianceType.WATER_HEATER: 1,
    ApplianceType.WASHING_MACHINE: 1,
    ApplianceType.AIR_CONDITIONER: 3,
    ApplianceType.EV_CHARGER: 4,
    ApplianceType.WATER_PUMP: 1,
}
_DEVICE_TYPE_TO_APPLIANCE = {
    "water_heater": ApplianceType.WATER_HEATER,
    "geyser": ApplianceType.WATER_HEATER,
    "washing_machine": ApplianceType.WASHING_MACHINE,
    "air_conditioner": ApplianceType.AIR_CONDITIONER,
    "ev_charger": ApplianceType.EV_CHARGER,
    "water_pump": ApplianceType.WATER_PUMP,
}
_MAX_APPLIANCES = 3

WeatherFactory = Callable[[], WeatherForecastPort | None]


def goal_for(primary_goal: str | None) -> Goal:
    text = (primary_goal or "").lower()
    if "backup" in text or "resilien" in text:
        return Goal.MAXIMIZE_BACKUP
    if "bill" in text or "cost" in text or "saving" in text:
        return Goal.REDUCE_BILL
    return Goal.MAXIMIZE_SELF_CONSUMPTION


def appliances_for(profile, question: str) -> tuple[list[ApplianceSpec], list[str]]:
    """Shiftable appliances to place: the one named in the question, else up to three controllable ones the home has."""
    notes: list[str] = []
    named = detect_appliance(question)
    candidates = []
    for device in profile.devices:
        if device.critical or not device.controllable or device.rated_power_kw <= 0:
            continue
        kind = _DEVICE_TYPE_TO_APPLIANCE.get(device.device_type)
        if kind is None or kind not in APPLIANCE_HOURS:
            continue
        candidates.append((device, kind))
    if named is not None:
        candidates = [(d, k) for d, k in candidates if k is named]
        if not candidates:
            notes.append(f"No controllable {named.value.replace('_', ' ')} is set up for this home, so none was scheduled.")
    specs = [
        ApplianceSpec(
            name=device.device_name.lower(),
            rated_kw=float(device.rated_power_kw),
            duration_slots=APPLIANCE_HOURS[kind],
            must_run=False,  # advice: if it cannot be placed it is skipped with a note, not a failure
        )
        for device, kind in candidates[:_MAX_APPLIANCES]
    ]
    return specs, notes


class OptimizerAgent:
    name = AgentName.OPTIMIZER

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
        profile, onboarding = self._data.profile(), self._data.onboarding()
        now = self._now()

        location = live.location if live is not None else None
        if live is None or location is None or location.latitude is None or location.longitude is None:
            return self._needs(started, "I need your home's location and recent readings to plan around the sun.")

        inputs = ForecastInputs(
            latitude=location.latitude,
            longitude=location.longitude,
            timezone=location.timezone or "UTC",
            solar_capacity_kwp=profile.solar_capacity_kwp,
            inverter_capacity_kw=profile.inverter_capacity_kw,
            history=history_points(records),
            horizon_hours=HORIZON_HOURS,
            observed_cloud_cover_pct=live.weather.cloud_cover_percent if live.weather else None,
            observed_at=live.timestamp if live.weather else None,
        )
        port = self._weather() or PersistenceWeather(
            observed_cloud_pct=inputs.observed_cloud_cover_pct, observed_at=inputs.observed_at
        )
        outlook = await forecast(inputs, port, now)

        try:
            selection = table_for(
                onboarding.discom, CATEGORY, now.date(),
                flat_rate=profile.tariff_rate, flat_export_credit=profile.export_credit_inr_per_kwh or 0,
            )
        except TariffNotFound:
            return self._needs(started, "I do not have a tariff to price your energy on.")
        table = selection.table
        if onboarding.avg_monthly_bill_inr is None and profile.tariff_rate is None:
            return self._needs(started, "I need your tariff or your typical monthly bill to know what energy costs.")
        usage, _ = usage_from_bill(table, Decimal(str(onboarding.avg_monthly_bill_inr or 0)))
        prices = marginal_prices(table, usage)

        assumptions: dict[str, object] = {}
        warnings = list(outlook.warnings)
        flat_load = live.load.power_kw if getattr(live, "load", None) is not None else 0.0
        slots: list[SlotInput] = []
        for slot in outlook.slots:
            band = band_at(table, slot.start)
            price = prices.get(band.name if band else None, next(iter(prices.values())))
            load = slot.load_kw_p50 if slot.load_kw_p50 is not None else flat_load
            slots.append(
                SlotInput(
                    start=slot.start,
                    solar_kw=slot.solar_kw_p50,
                    base_load_kw=load,
                    import_price_inr_per_kwh=price,
                    export_credit_inr_per_kwh=export_credit(table, price),
                )
            )
        if outlook.methods.load == "unknown":
            warnings.append("Your usage pattern is not known yet, so the current load was assumed for every hour.")
            assumptions["assumed_flat_load_kw"] = round(flat_load, 2)

        sanctioned = onboarding.sanctioned_load_kw or DEFAULT_SANCTIONED_LOAD_KW
        if onboarding.sanctioned_load_kw is None:
            assumptions["assumed_sanctioned_load_kw"] = DEFAULT_SANCTIONED_LOAD_KW
            warnings.append("Your sanctioned load is not set, so a typical value was assumed.")

        battery = None
        if profile.battery_present and live.battery is not None:
            battery = BatterySpec(
                usable_capacity_kwh=profile.battery_usable_capacity_kwh,
                current_soc_percent=live.battery.soc_percent,
                max_charge_kw=profile.battery_max_charge_power_kw,
                max_discharge_kw=profile.battery_max_discharge_power_kw,
                min_soc_percent=profile.battery_minimum_soc_percent,
            )
        appliances, notes = appliances_for(profile, call.question)
        for spec in appliances:
            assumptions[f"assumed_{spec.name.replace(' ', '_')}_run_hours"] = spec.duration_slots

        problem = OptimizationProblem(
            slots=tuple(slots),
            grid=GridSpec(
                sanctioned_load_kw=sanctioned,
                zero_export=profile.zero_export_mode,
                inverter_capacity_kw=profile.inverter_capacity_kw,
            ),
            battery=battery,
            appliances=tuple(appliances),
            goal=goal_for(profile.primary_goal),
        )
        result = optimize(problem)
        if result.status is Status.INFEASIBLE:
            return self._needs(started, "I could not find a schedule that respects your battery and grid limits.")

        facts = result.to_facts()
        facts["tariff"] = selection.to_facts()
        facts["forecast_confidence"] = outlook.confidence.level
        if assumptions:
            facts["assumptions"] = assumptions
        if notes:
            facts["notes"] = notes
        if not table.verified:
            warnings.append(
                "UNVERIFIED TARIFF: prices come from an illustrative or profile-derived table, so rupee figures are estimates."
            )
        warnings.extend(w for w in result.warnings if w not in warnings)
        # Never "high": the forecast is an estimate, and the shipped tariff tables are illustrative.
        confidence = "low" if outlook.confidence.level == "low" else "medium"

        return AgentResult(
            agent=self.name,
            status=AgentStatus.OK,
            facts={"optimizer": facts},
            freshness=assess_freshness(live.timestamp, now=now, threshold_seconds=self._stale).model_dump(mode="json"),
            analytics_used=["schedule_optimization", "solar_forecast", "tariff_prices"],
            draft=_draft(facts),
            warnings=warnings,
            confidence=confidence,  # type: ignore[arg-type]
            live_advice=True,
            requires_telemetry=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _needs(self, started: float, why: str) -> AgentResult:
        return AgentResult(
            agent=self.name,
            status=AgentStatus.PARTIAL,
            draft=AnswerParts(
                observation=why,
                explanation="A schedule built on guesses could cost you money, so I did not make one.",
                recommendation="Check your home's setup (location, tariff or bill, battery) and that data is arriving, then ask again.",
                estimated_impact="Not enough data to estimate.",
            ),
            fallback_reason=NEEDS_INPUT_REASON,
            confidence="low",
            live_advice=False,
            requires_telemetry=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _draft(facts: dict) -> AnswerParts:
    """Plain wording from the solver's own summary lines. Used only if the model is switched off."""
    summary = [s for s in facts.get("plan_summary", []) if isinstance(s, str)]
    head = summary[0] if summary else "Your plan for the next day keeps things as they are."
    rest = " ".join(summary[1:3])
    return AnswerParts(
        observation=head,
        explanation=rest or "The plan follows the solar forecast and your tariff.",
        recommendation="Follow the schedule if it suits you. Nothing is changed on your devices unless you ask.",
        estimated_impact="Rupee figures are estimates based on the tariff used.",
    )
