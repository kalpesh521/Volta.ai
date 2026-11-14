"""
Monthly usage for a bill estimate, and the what-if scenarios a question asks for.

The assistant has the home's recent telemetry (hours to a few days), not a month of meter readings, so a monthly
figure has to come from somewhere honest. The order, and what each says about itself:

1. the user's own **stated average monthly bill** (onboarding), turned back into units by inverting the bill
   calculator on the tariff in force (`usage_from_bill`). Marked as an estimate; it can only be as good as the
   stated bill and the table.
2. nothing else. Extrapolating a few hours of telemetry to a month would look precise and be wrong, so without a
   stated bill the agent says what it needs instead of inventing units.

Scenario parsing is rules, not a model: the numbers in a what-if ("2 kWp", "5 kWh battery", "cut 10%") are read
from the question with narrow patterns, and every default the simulator needs (yield, self-consumption share,
battery efficiency) is a named constant that is also placed in the facts, so the answer can state it and the
grounding guard can find it.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from app.agents_platform.agents.bill_tariff.calculator import calculate_bill
from app.agents_platform.agents.bill_tariff.models import MonthlyUsage, TariffTable
from app.agents_platform.agents.bill_tariff.whatif import (
    AddBattery,
    AddSolar,
    ReduceConsumption,
    Scenario,
    ShiftLoad,
)

# Assumptions the what-if needs. Visible in the facts, never hidden in code.
SOLAR_YIELD_KWH_PER_KWP_MONTH = Decimal("120")  # about 4 kWh per kWp per day, a common Indian planning figure
SOLAR_SELF_CONSUMPTION_SHARE = Decimal("0.7")
BATTERY_USABLE_FRACTION = Decimal("0.9")
BATTERY_ROUND_TRIP_EFFICIENCY = Decimal("0.9")
BATTERY_CYCLES_PER_MONTH = Decimal("25")
BATTERY_SHIFTABLE_SHARE = Decimal("0.5")
LOAD_SHIFT_SHARE_OF_IMPORT = Decimal("0.15")

MAX_SEARCH_KWH = Decimal("5000")
_SEARCH_STEPS = 48

_NUM = r"(\d+(?:\.\d+)?)"
_SOLAR = re.compile(rf"{_NUM}\s*k\s*w\s*p?\b(?:\s+of)?(?:\s+(?:extra|more|additional|new))?\s*(?:solar|panels?|pv)?", re.I)
_BATTERY = re.compile(rf"{_NUM}\s*k\s*w\s*h\b", re.I)
_PERCENT = re.compile(rf"{_NUM}\s*(?:%|per\s*cent|percent)", re.I)
_WANTS_WHAT_IF = re.compile(
    r"\bwhat\s+if\b|\bhow\s+much\s+would\b|\bif\s+i\s+(?:add|install|get|buy|shift|move|cut|reduce)\b|"
    r"\b(?:add|install|get|buy)\w*\b.*\b(?:solar|battery|panels?|kwp|kwh)\b|\bpay\s*-?\s*back\b|"
    r"\bshift\w*\b.*\b(?:load|usage)\b|\b(?:cut|reduce|lower)\w*\b.*\b(?:consumption|usage|units)\b",
    re.I,
)


def wants_what_if(question: str) -> bool:
    return bool(_WANTS_WHAT_IF.search(question))


def usage_from_bill(table: TariffTable, bill_inr: Decimal, *, days: int = 30) -> tuple[MonthlyUsage, bool]:
    """
    Import units that would produce `bill_inr` on `table`. Bisection on the monotone bill function.
    The bool is True when the bill is above anything the search covers (so the figure is a floor, not a match).
    """
    def total(kwh: Decimal) -> Decimal:
        return calculate_bill(MonthlyUsage(import_kwh=kwh, days=days), table).total_inr

    if bill_inr <= total(Decimal("0")):
        return MonthlyUsage(import_kwh=Decimal("0"), days=days), False
    if total(MAX_SEARCH_KWH) < bill_inr:
        return MonthlyUsage(import_kwh=MAX_SEARCH_KWH, days=days), True
    low, high = Decimal("0"), MAX_SEARCH_KWH
    for _ in range(_SEARCH_STEPS):
        mid = (low + high) / 2
        if total(mid) < bill_inr:
            low = mid
        else:
            high = mid
    return MonthlyUsage(import_kwh=high.quantize(Decimal("0.1")), days=days), False


def _peak_and_cheapest(table: TariffTable) -> tuple[str, str] | None:
    bands = table.tod_bands or []
    if len(bands) < 2:
        return None
    ordered = sorted(bands, key=lambda b: b.multiplier)
    return ordered[-1].name, ordered[0].name


def scenarios_for(
    question: str, usage: MonthlyUsage, table: TariffTable
) -> tuple[list[Scenario], list[str]]:
    """The what-if scenarios the question names, and notes about anything asked for that could not be built."""
    scenarios: list[Scenario] = []
    notes: list[str] = []
    text = question.lower()

    if re.search(r"\b(?:solar|panels?|pv|kwp)\b", text) and (match := _SOLAR.search(question)):
        scenarios.append(
            AddSolar(
                label="add_solar",
                kwp=Decimal(match.group(1)),
                yield_kwh_per_kwp_month=SOLAR_YIELD_KWH_PER_KWP_MONTH,
                self_consumption_share=SOLAR_SELF_CONSUMPTION_SHARE,
            )
        )
    if re.search(r"\bbatter(?:y|ies)\b", text) and (match := _BATTERY.search(question)):
        bands = _peak_and_cheapest(table)
        if usage.export_kwh > 0:
            strategy = dict(strategy="absorb_exports")
        elif bands is not None:
            strategy = dict(
                strategy="shift_import", from_band=bands[0], to_band=bands[1], shiftable_share=BATTERY_SHIFTABLE_SHARE
            )
        else:
            strategy = None
            notes.append("A battery saves nothing on this tariff: it has no time-of-day rates and your usage has no exports.")
        if strategy is not None:
            scenarios.append(
                AddBattery(
                    label="add_battery",
                    capacity_kwh=Decimal(match.group(1)),
                    usable_fraction=BATTERY_USABLE_FRACTION,
                    round_trip_efficiency=BATTERY_ROUND_TRIP_EFFICIENCY,
                    cycles_per_month=BATTERY_CYCLES_PER_MONTH,
                    **strategy,
                )
            )
    if re.search(r"\b(?:cut|reduce|lower|save)\w*\b", text) and re.search(r"\b(?:consumption|usage|units|electricity)\b", text):
        if match := _PERCENT.search(question):
            percent = Decimal(match.group(1))
            if 0 < percent <= 100:
                scenarios.append(ReduceConsumption(label="reduce_consumption", percent=percent))
    if re.search(r"\bshift\w*\b", text) and re.search(r"\b(?:load|usage|appliances)\b", text):
        bands = _peak_and_cheapest(table)
        if bands is None:
            notes.append("Shifting load saves nothing on this tariff: it has no time-of-day rates.")
        elif usage.import_by_band is None:
            peak_kwh = usage.import_kwh * LOAD_SHIFT_SHARE_OF_IMPORT
            if peak_kwh > 0:
                scenarios.append(ShiftLoad(label="shift_load", kwh=peak_kwh, from_band=bands[0], to_band=bands[1]))
    return scenarios, notes


def assumptions_facts(scenarios: Sequence[Scenario]) -> dict[str, float]:
    """The constants behind the scenarios actually run, as numbers the answer may quote."""
    kinds = {s.kind for s in scenarios}
    facts: dict[str, float] = {}
    if "add_solar" in kinds:
        facts["assumed_solar_yield_kwh_per_kwp_month"] = float(SOLAR_YIELD_KWH_PER_KWP_MONTH)
        facts["assumed_solar_self_consumption_percent"] = float(SOLAR_SELF_CONSUMPTION_SHARE * 100)
    if "add_battery" in kinds:
        facts["assumed_battery_efficiency_percent"] = float(BATTERY_ROUND_TRIP_EFFICIENCY * 100)
        facts["assumed_battery_cycles_per_month"] = float(BATTERY_CYCLES_PER_MONTH)
    if "shift_load" in kinds:
        facts["assumed_load_shift_percent_of_import"] = float(LOAD_SHIFT_SHARE_OF_IMPORT * 100)
    return facts
