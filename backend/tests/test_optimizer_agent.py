"""The Optimizer agent: forecast + tariff + solver joined, with every assumption stated and nothing invented."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agents_platform.agents.bill_tariff import MonthlyUsage, default_registry, flat_table
from app.agents_platform.agents.optimizer import Goal
from app.agents_platform.agents.optimizer.agent import (
    NEEDS_INPUT_REASON,
    OptimizerAgent,
    appliances_for,
    goal_for,
)
from app.agents_platform.agents.optimizer.pricing import band_at, export_credit, marginal_prices
from app.agents_platform.agents.planning_data import AuthorizedData
from app.agents_platform.contracts import AgentCall, AgentName, AgentStatus
from app.agents_platform.guardrails.policy import DataForbiddenError
from app.agents_platform.orchestration.retry import TransientAgentError
from app.modules.assistant.domain.grounding import check_grounding
from tests.test_forecast_agent import NOW, PUNE, day_of_history, live as base_live

IST = ZoneInfo("Asia/Kolkata")


def device(name, kind, kw, *, critical=False, controllable=True):
    return SimpleNamespace(device_name=name, device_type=kind, rated_power_kw=kw, critical=critical, controllable=controllable)


DEVICES = [
    device("Washing machine", "washing_machine", 0.5),
    device("Water heater", "water_heater", 2.0),
    device("Refrigerator", "refrigerator", 0.15, critical=True, controllable=False),
    device("Air conditioner", "air_conditioner", 1.5, controllable=False),
]


def full_live(location=PUNE, soc=50.0):
    value = base_live(location)
    value.battery = SimpleNamespace(soc_percent=soc)
    value.load = SimpleNamespace(power_kw=1.1)
    return value


class FakeData:
    def __init__(self, *, live_value=None, bill=2400.0, sanctioned=5.0, battery=True, devices=None, tariff_rate=8.5,
                 discom=None, goal="reduce_bill", error=None, records=None):
        self._live = full_live() if live_value is None else live_value
        self._records = day_of_history() if records is None else records
        self._error = error
        self._profile = SimpleNamespace(
            solar_capacity_kwp=4.0, inverter_capacity_kw=5.0, battery_present=battery,
            battery_usable_capacity_kwh=9.0, battery_max_charge_power_kw=5.0, battery_max_discharge_power_kw=5.0,
            battery_minimum_soc_percent=20.0, zero_export_mode=False, primary_goal=goal, tariff_rate=tariff_rate,
            export_credit_inr_per_kwh=3.0, devices=DEVICES if devices is None else devices,
        )
        self._onboarding = SimpleNamespace(avg_monthly_bill_inr=bill, sanctioned_load_kw=sanctioned, discom=discom)
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
        return self._profile

    def onboarding(self):
        return self._onboarding


def call(question="Optimize my battery usage") -> AgentCall:
    return AgentCall(agent=AgentName.OPTIMIZER, question=question, intents=("optimize",), deadline_seconds=5)


def agent(data=None, **kw) -> OptimizerAgent:
    return OptimizerAgent(data or FakeData(**kw), now=lambda: NOW, stale_threshold_seconds=600)


# --- pricing --------------------------------------------------------------------------------------


def tod_table():
    return next(t for t in default_registry().tables if t.tod_bands)


def test_the_peak_band_costs_more_than_the_night_band_by_the_tariffs_own_multipliers():
    table = tod_table()
    prices = marginal_prices(table, MonthlyUsage(import_kwh=Decimal("300")))
    by_multiplier = sorted(table.tod_bands, key=lambda b: b.multiplier)
    assert prices[by_multiplier[-1].name] > prices[by_multiplier[0].name] > 0


def test_a_tariff_without_bands_has_one_price_that_includes_duty_and_fuel_adjustment():
    table = flat_table(8.5, 0)
    prices = marginal_prices(table, MonthlyUsage(import_kwh=Decimal("300")))
    assert list(prices) == [None] and prices[None] >= 8.5


def test_the_marginal_price_is_what_the_calculator_charges_for_one_more_unit():
    from app.agents_platform.agents.bill_tariff import calculate_bill

    table = flat_table(8.5, 0)
    usage = MonthlyUsage(import_kwh=Decimal("300"))
    expected = calculate_bill(usage.model_copy(update={"import_kwh": Decimal("301")}), table).total_inr - calculate_bill(usage, table).total_inr
    assert marginal_prices(table, usage)[None] == pytest.approx(float(expected))


def test_each_hour_falls_in_exactly_one_band_including_the_one_that_wraps_midnight():
    table = tod_table()
    seen = {band_at(table, datetime(2026, 11, 14, h, 0, tzinfo=IST)).name for h in range(24)}
    assert seen == {b.name for b in table.tod_bands}
    assert all(band_at(table, datetime(2026, 11, 14, h, 30, tzinfo=IST)) is not None for h in range(24))


def test_export_credit_follows_the_tariffs_export_rules():
    registry = default_registry()
    metered = next(t for t in registry.tables if t.export.mode == "net_metering")
    billed = next(t for t in registry.tables if t.export.mode == "net_billing")
    assert export_credit(metered, 9.0) == 9.0
    assert export_credit(billed, 9.0) == float(billed.export.credit_per_kwh)
    assert export_credit(flat_table(8.5, 0).model_copy(update={"export": metered.export.model_copy(update={"mode": "none"})}), 9.0) == 0.0


# --- the agent ------------------------------------------------------------------------------------


async def test_a_plan_comes_back_as_the_solvers_facts_with_the_tariff_flag_and_forecast_confidence():
    result = await agent().run(call())
    facts = result.facts["optimizer"]

    assert result.status is AgentStatus.OK and result.agent is AgentName.OPTIMIZER
    assert facts["plan_summary"] and facts["tariff"]["tariff_verified"] is False
    assert facts["forecast_confidence"] in ("high", "medium", "low")
    assert any("UNVERIFIED TARIFF" in w for w in result.warnings)
    assert result.confidence in ("medium", "low") and result.live_advice and result.requires_telemetry
    assert result.freshness["status"] == "fresh"
    assert {"schedule_optimization", "solar_forecast", "tariff_prices"} <= set(result.analytics_used)


async def test_it_never_claims_high_confidence_because_the_forecast_and_the_tariff_are_estimates():
    assert (await agent().run(call())).confidence != "high"


async def test_the_appliances_in_the_plan_are_the_controllable_non_critical_ones_the_home_has():
    facts = (await agent().run(call())).facts["optimizer"]
    names = {a["name"] for a in facts.get("appliances", [])}
    assert names <= {"washing machine", "water heater"} and names
    assert "refrigerator" not in names and "air conditioner" not in names


async def test_a_named_appliance_is_the_only_one_scheduled():
    facts = (await agent().run(call("When is the best time to run the washing machine?"))).facts["optimizer"]
    assert {a["name"] for a in facts["appliances"]} == {"washing machine"}


async def test_a_named_appliance_the_home_does_not_have_is_explained_not_invented():
    facts = (await agent(devices=[device("Fridge", "refrigerator", 0.15, critical=True, controllable=False)]).run(
        call("Best time to charge my EV?")
    )).facts["optimizer"]
    assert "appliances" not in facts or not facts["appliances"]
    assert any("No controllable ev charger" in n for n in facts["notes"])


async def test_assumptions_are_stated_as_numbers_the_answer_may_quote():
    facts = (await agent(sanctioned=None, records=[]).run(call())).facts["optimizer"]
    assert facts["assumptions"]["assumed_sanctioned_load_kw"] == 5.0
    assert "assumed_flat_load_kw" in facts["assumptions"]
    assert facts["assumptions"]["assumed_washing_machine_run_hours"] == 1


async def test_a_home_without_a_battery_still_gets_an_appliance_schedule():
    result = await agent(battery=False).run(call())
    assert result.status is AgentStatus.OK and "battery" not in result.facts["optimizer"]


async def test_a_plan_can_never_cost_more_than_doing_nothing_for_a_bill_goal():
    facts = (await agent(goal="reduce_bill").run(call())).facts["optimizer"]
    assert facts["plan_cost_inr"] <= facts["baseline_cost_inr"] + 0.01 and facts["savings_inr"] >= -0.01


async def test_missing_location_or_tariff_basis_says_what_is_needed():
    from app.modules.energy.schemas import HomeLocationOut

    no_place = await agent(FakeData(live_value=full_live(HomeLocationOut(name="x")))).run(call())
    assert no_place.fallback_reason == NEEDS_INPUT_REASON and no_place.answers_itself and no_place.facts == {}
    no_data = await agent(FakeData(live_value=False)).run(call())
    assert no_data.fallback_reason == NEEDS_INPUT_REASON
    no_price = await agent(FakeData(bill=None, tariff_rate=None)).run(call())
    assert no_price.fallback_reason == NEEDS_INPUT_REASON and "tariff" in no_price.draft.observation


async def test_a_flaky_store_is_transient_and_a_bug_is_not():
    with pytest.raises(TransientAgentError):
        await agent(FakeData(error=ConnectionError("reset"))).run(call())
    with pytest.raises(RuntimeError):
        await agent(FakeData(error=RuntimeError("bug"))).run(call())


async def test_the_draft_the_model_may_fall_back_to_is_grounded_in_the_facts():
    result = await agent().run(call())
    grounding = check_grounding(result.draft.model_dump(), facts=[result.facts])
    assert grounding.ok, grounding.ungrounded


async def test_the_agent_reads_only_the_data_its_job_needs():
    data = FakeData()
    result = await agent(AuthorizedData(AgentName.OPTIMIZER, data)).run(call())
    assert result.status is AgentStatus.OK and set(data.reads) == {"live", "history", "profile"}
    with pytest.raises(DataForbiddenError):
        await AuthorizedData(AgentName.BILL_TARIFF, data).live()


# --- small helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal, expected",
    [
        ("reduce_bill", Goal.REDUCE_BILL),
        ("Reduce electricity cost", Goal.REDUCE_BILL),
        ("maximize_backup", Goal.MAXIMIZE_BACKUP),
        ("backup_priority", Goal.MAXIMIZE_BACKUP),
        ("maximize_self_consumption", Goal.MAXIMIZE_SELF_CONSUMPTION),
        (None, Goal.MAXIMIZE_SELF_CONSUMPTION),
    ],
)
def test_the_homes_goal_maps_to_a_solver_goal(goal, expected):
    assert goal_for(goal) is expected


def test_at_most_three_appliances_and_never_a_critical_or_uncontrollable_one():
    many = [device(f"Pump {i}", "water_pump", 0.5) for i in range(6)] + DEVICES
    specs, _ = appliances_for(SimpleNamespace(devices=many), "optimize my day")
    assert len(specs) == 3 and all(s.rated_kw > 0 and not s.must_run for s in specs)
