"""The Bill & Tariff agent: usage from a stated bill, what-if parsing, the honesty flags, entitlements and data scopes."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agents_platform.agents.bill_tariff import (
    MonthlyUsage,
    calculate_bill,
    default_registry,
    flat_table,
)
from app.agents_platform.agents.bill_tariff.agent import NEEDS_BASIS_REASON, BillTariffAgent
from app.agents_platform.agents.bill_tariff.usage import (
    MAX_SEARCH_KWH,
    assumptions_facts,
    scenarios_for,
    usage_from_bill,
    wants_what_if,
)
from app.agents_platform.agents.planning_data import AuthorizedData
from app.agents_platform.contracts import AgentCall, AgentName, AgentStatus
from app.agents_platform.guardrails.policy import DataForbiddenError
from app.modules.assistant.domain.context import OnboardingDetails
from app.modules.assistant.domain.grounding import check_grounding
from app.modules.energy.schemas import SimulatorProfileOut

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def profile(**overrides) -> SimulatorProfileOut:
    base = dict(
        household_id="home_001", system_type="hybrid", panel_type="mono", panel_qty=10, solar_capacity_kwp=5.0,
        inverter_capacity_kw=5.0, battery_present=True, battery_capacity_kwh=10.0, battery_usable_capacity_kwh=9.0,
        battery_minimum_soc_percent=20.0, battery_max_charge_power_kw=5.0, battery_max_discharge_power_kw=5.0,
        grid_available=True, zero_export_mode=False, tariff_rate=8.5, export_credit_inr_per_kwh=3.0,
    )
    return SimulatorProfileOut(**{**base, **overrides})


class FakeData:
    def __init__(self, *, bill: float | None = 2000.0, discom: str | None = None, **profile_overrides) -> None:
        self._profile = profile(**profile_overrides)
        self._onboarding = OnboardingDetails(avg_monthly_bill_inr=bill, discom=discom)
        self.live_reads = 0

    async def live(self):
        self.live_reads += 1

    async def history(self, limit: int):
        return []

    def profile(self):
        return self._profile

    def onboarding(self):
        return self._onboarding


def call(question: str) -> AgentCall:
    return AgentCall(agent=AgentName.BILL_TARIFF, question=question, intents=("bill_estimate",), deadline_seconds=5)


def agent(data=None, *, what_if=True, **kw) -> BillTariffAgent:
    return BillTariffAgent(data or FakeData(**kw), now=lambda: NOW, what_if_allowed=what_if)


# --- usage from a stated bill ---------------------------------------------------------------------


@pytest.mark.parametrize("bill", [300, 1200, 2000, 4500])
def test_the_units_estimated_from_a_bill_reproduce_that_bill(bill):
    table = flat_table(8.5, 3.0)
    usage, capped = usage_from_bill(table, Decimal(bill))
    assert not capped
    assert abs(calculate_bill(usage, table).total_inr - Decimal(bill)) <= Decimal("1.0")


def test_a_bill_beyond_what_the_search_covers_is_flagged_as_a_floor():
    usage, capped = usage_from_bill(flat_table(8.5, 0), Decimal("10000000"))
    assert capped and usage.import_kwh == MAX_SEARCH_KWH


def test_a_bill_of_nothing_is_no_usage():
    usage, capped = usage_from_bill(flat_table(8.5, 0), Decimal("0"))
    assert usage.import_kwh == 0 and not capped


def test_more_bill_means_more_units():
    table = default_registry().tables[0]
    low, _ = usage_from_bill(table, Decimal("800"))
    high, _ = usage_from_bill(table, Decimal("2400"))
    assert high.import_kwh > low.import_kwh


# --- the agent ------------------------------------------------------------------------------------


async def test_a_bill_estimate_is_itemised_and_says_the_tariff_is_not_official():
    result = await agent().run(call("Estimate my bill next month"))
    facts = result.facts["bill_tariff"]

    assert result.status is AgentStatus.OK and result.agent is AgentName.BILL_TARIFF
    assert facts["tariff"]["tariff_verified"] is False and facts["tariff"]["tariff_is_fallback"] is True
    assert facts["bill"]["total_inr"] > 0 and facts["bill"]["import_kwh"] == facts["usage_basis"]["estimated_import_kwh"]
    assert facts["usage_basis"]["source"] == "your stated average monthly bill"
    assert any("unverified" in w.lower() or "illustrative" in w.lower() for w in result.warnings)
    assert result.confidence == "medium" and not result.requires_telemetry and not result.live_advice
    assert "not an official rate" in result.draft.observation


async def test_the_estimate_reproduces_the_users_stated_bill_to_the_rupee():
    result = await agent(bill=1800.0).run(call("Estimate my bill next month"))
    assert abs(result.facts["bill_tariff"]["bill"]["total_inr"] - 1800.0) <= 1.0


async def test_without_a_stated_bill_it_says_what_it_needs_instead_of_guessing():
    result = await agent(bill=None).run(call("Estimate my bill next month"))
    assert result.status is AgentStatus.PARTIAL and result.fallback_reason == NEEDS_BASIS_REASON
    assert result.answers_itself and result.facts == {} and result.confidence == "low"
    assert "monthly electricity bill" in result.draft.observation


async def test_an_unknown_discom_falls_back_to_the_users_own_flat_rate_and_says_so():
    facts = (await agent(discom="Some Unknown Power Co").run(call("Estimate my bill next month"))).facts["bill_tariff"]
    assert facts["tariff"]["tariff_is_fallback"] is True and facts["tariff"]["tariff_id"] == "flat-profile"
    assert "unknown DISCOM" in facts["tariff"]["tariff_selection_reason"] and "flat rate" in facts["tariff"]["tariff_selection_reason"]


async def test_a_what_if_runs_the_scenario_and_lists_every_assumption_it_used():
    result = await agent().run(call("What if I add 2 kWp of solar, how much would my bill drop?"))
    facts = result.facts["bill_tariff"]

    assert len(facts["what_if"]) == 1 and facts["what_if"][0]["saving_inr_month"] > 0
    assert facts["assumptions"]["assumed_solar_yield_kwh_per_kwp_month"] == 120.0
    assert facts["assumptions"]["assumed_solar_self_consumption_percent"] == 70.0
    assert "add_solar" in result.analytics_used


async def test_a_what_if_on_a_plan_that_does_not_include_it_gets_the_plain_estimate_and_a_reason():
    result = await agent(what_if=False).run(call("What if I add 2 kWp of solar, how much would my bill drop?"))
    facts = result.facts["bill_tariff"]
    assert "what_if" not in facts and facts["bill"]["total_inr"] > 0
    assert any("Plus plan" in w for w in result.warnings)


async def test_a_plain_estimate_is_never_held_back_by_the_what_if_rule():
    result = await agent(what_if=False).run(call("Estimate my bill next month"))
    assert not any("Plus plan" in w for w in result.warnings)


async def test_a_battery_on_a_flat_tariff_with_no_exports_is_explained_not_faked():
    result = await agent().run(call("What if I add a 5 kWh battery?"))
    facts = result.facts["bill_tariff"]
    assert "what_if" not in facts and any("saves nothing" in n for n in facts["notes"])


async def test_the_draft_the_model_may_fall_back_to_is_grounded_in_the_facts():
    result = await agent().run(call("What if I add 2 kWp of solar, how much would my bill drop?"))
    grounding = check_grounding(result.draft.model_dump(), facts=[result.facts])
    assert grounding.ok, grounding.ungrounded


async def test_the_agent_reads_only_the_data_its_job_needs():
    data = FakeData()
    result = await agent(AuthorizedData(AgentName.BILL_TARIFF, data)).run(call("Estimate my bill next month"))
    assert result.status is AgentStatus.OK and data.live_reads == 0
    with pytest.raises(DataForbiddenError):
        await AuthorizedData(AgentName.BILL_TARIFF, data).live()


# --- scenario parsing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question, expected",
    [
        ("What if I add 2 kWp of solar?", True),
        ("How much would my bill be with a battery?", True),
        ("Is solar payback worth it", True),
        ("Estimate my bill next month", False),
        ("Explain my electricity bill", False),
    ],
)
def test_what_if_phrasing_is_recognised(question, expected):
    assert wants_what_if(question) is expected


def usage(**kw) -> MonthlyUsage:
    return MonthlyUsage(import_kwh=Decimal("400"), **kw)


def tod_table():
    registry = default_registry()
    return next(t for t in registry.tables if t.tod_bands)


def test_the_numbers_in_a_scenario_come_from_the_question():
    table = flat_table(8.5, 0)
    found, _ = scenarios_for("what if I add 3.5 kWp solar", usage(), table)
    assert [s.kind for s in found] == ["add_solar"] and found[0].kwp == Decimal("3.5")
    found, _ = scenarios_for("what if I cut my electricity usage by 12%", usage(), table)
    assert found[0].kind == "reduce_consumption_percent" and found[0].percent == Decimal("12")


def test_a_percentage_outside_zero_to_a_hundred_is_ignored():
    found, _ = scenarios_for("what if I cut my usage by 150%", usage(), flat_table(8.5, 0))
    assert found == []


def test_a_battery_shifts_peak_to_the_cheapest_band_when_the_tariff_has_time_of_day_rates():
    table = tod_table()
    found, notes = scenarios_for("what if I add a 5 kWh battery", usage(), table)
    assert found and found[0].strategy == "shift_import" and notes == []
    bands = sorted(table.tod_bands, key=lambda b: b.multiplier)
    assert found[0].from_band == bands[-1].name and found[0].to_band == bands[0].name


def test_a_battery_absorbs_exports_when_there_are_exports():
    found, _ = scenarios_for("what if I add a 5 kWh battery", usage(export_kwh=Decimal("90")), flat_table(8.5, 3))
    assert found[0].strategy == "absorb_exports"


def test_assumptions_are_reported_only_for_scenarios_that_ran():
    table = flat_table(8.5, 0)
    solar, _ = scenarios_for("what if I add 2 kWp solar", usage(), table)
    assert set(assumptions_facts(solar)) == {"assumed_solar_yield_kwh_per_kwp_month", "assumed_solar_self_consumption_percent"}
    assert assumptions_facts([]) == {}


def test_a_scenario_never_spans_into_another_users_data():
    # the agent is constructed with one household's data port and takes no household argument anywhere
    import inspect

    assert "household_id" not in inspect.signature(BillTariffAgent.run).parameters
    assert "household_id" not in inspect.signature(BillTariffAgent.__init__).parameters
