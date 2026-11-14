"""
What-if simulator: change one thing about a home's month and price the difference.

Design rules:
  * No hidden assumptions. Every number that is a guess (solar yield, self-consumption share, battery
    efficiency, cycles) is a REQUIRED field on the scenario, and every result lists the assumptions it
    used as plain sentences. The caller (or the assistant) can always say "this assumes X".
  * The model never computes. Savings per month, per year, new bill and payback are all fields, and
    `to_facts()` returns them rounded, so a grounding guard can match every number in an answer.
  * One month is one snapshot. "Per year" is exactly 12 x the monthly saving (no seasonality), and units
    banked at month end are reported but not valued. These limits are stated in the assumptions.

Sign convention: `delta_*` = scenario minus baseline (negative = less). `saving_inr_month` = -delta, so a
positive saving is good. Payback is capex / monthly saving, None unless both are positive.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents_platform.agents.bill_tariff.calculator import ZERO, calculate_bill
from app.agents_platform.agents.bill_tariff.models import (
    BillBreakdown,
    MonthlyUsage,
    NonNegative,
    Number,
    TariffTable,
    to_float,
)

MONTHS_PER_YEAR = Decimal(12)


def _n(value: Decimal, places: int = 2) -> str:
    """Decimal as a short sentence number: '80', '12.5', never '80.00' or '1E+2'."""
    return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP).normalize(), "f")
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
PositiveFraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class ScenarioError(ValueError):
    """The scenario cannot be applied to this usage/table (e.g. a ToD shift on a table without ToD)."""


class _Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str | None = None
    capex_inr: NonNegative | None = None


class AddSolar(_Scenario):
    """Extra PV. Generation = kwp x yield. `self_consumption_share` of it offsets imports (capped at the
    imports available); the rest is exported. Offset import is removed pro rata across ToD bands."""

    kind: Literal["add_solar"] = "add_solar"
    kwp: Positive
    yield_kwh_per_kwp_month: Positive
    self_consumption_share: Fraction


class AddBattery(_Scenario):
    """
    A battery with monthly throughput = capacity x usable_fraction x cycles_per_month.

    strategy `absorb_exports`: stores exports and replays them later, losing (1 - efficiency) on the way.
    strategy `shift_import`: discharges in `from_band` and recharges from the grid in `to_band`; moves at
    most `shiftable_share` of the from_band import, and the extra grid draw in to_band covers the losses.
    """

    kind: Literal["add_battery"] = "add_battery"
    capacity_kwh: Positive
    usable_fraction: PositiveFraction
    round_trip_efficiency: PositiveFraction
    cycles_per_month: Annotated[Decimal, Field(gt=0, le=62, allow_inf_nan=False)]
    strategy: Literal["absorb_exports", "shift_import"]
    from_band: str | None = None
    to_band: str | None = None
    shiftable_share: Fraction | None = None

    @model_validator(mode="after")
    def _shift_needs_bands(self) -> "AddBattery":
        if self.strategy == "shift_import" and not (self.from_band and self.to_band and self.shiftable_share is not None):
            raise ValueError("shift_import needs from_band, to_band and shiftable_share")
        return self


class ShiftLoad(_Scenario):
    """Move up to `kwh` of import from one ToD band to another with no loss (the load is assumed movable)."""

    kind: Literal["shift_load"] = "shift_load"
    kwh: Positive
    from_band: str
    to_band: str


class ChangeTariff(_Scenario):
    """Same usage priced under `other_table`. Behaviour is assumed unchanged."""

    kind: Literal["change_tariff"] = "change_tariff"
    other_table: TariffTable


class ReduceConsumption(_Scenario):
    """Cut import by a percentage (efficiency, behaviour). Exports are unchanged."""

    kind: Literal["reduce_consumption_percent"] = "reduce_consumption_percent"
    percent: Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]


Scenario = Annotated[
    AddSolar | AddBattery | ShiftLoad | ChangeTariff | ReduceConsumption, Field(discriminator="kind")
]


class ScenarioResult(BaseModel):
    """Baseline vs scenario for one change. JSON-dumpable; `to_facts()` is the compact view for the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    baseline_total_inr: Number
    scenario_total_inr: Number
    delta_import_kwh: Number
    delta_export_kwh: Number
    delta_inr_month: Number
    delta_inr_year: Number
    saving_inr_month: Number
    saving_inr_year: Number
    capex_inr: Number | None
    payback_months: Number | None
    direction: Literal["saving", "extra_cost", "no_change"]
    assumptions: list[str]
    scenario_bill: BillBreakdown
    warnings: list[str]

    def to_facts(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "direction": self.direction,
            "baseline_bill_inr": to_float(self.baseline_total_inr),
            "new_bill_inr": to_float(self.scenario_total_inr),
            "saving_inr_month": to_float(self.saving_inr_month),
            "saving_inr_year": to_float(self.saving_inr_year),
            "delta_import_kwh": to_float(self.delta_import_kwh),
            "delta_export_kwh": to_float(self.delta_export_kwh),
            "capex_inr": None if self.capex_inr is None else to_float(self.capex_inr),
            "payback_months": None if self.payback_months is None else to_float(self.payback_months, 1),
            "new_bill_effective_rate_inr_per_kwh": None
            if self.scenario_bill.effective_rate_inr_per_kwh is None
            else to_float(self.scenario_bill.effective_rate_inr_per_kwh),
            "new_banked_units_kwh": to_float(self.scenario_bill.banked_units_out_kwh),
            "new_tariff_id": self.scenario_bill.tariff_id,
            "new_tariff_verified": self.scenario_bill.tariff_verified,
            "assumptions": list(self.assumptions),
        }


class SimulationResult(BaseModel):
    """Baseline bill plus one `ScenarioResult` per scenario, in input order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_bill: BillBreakdown
    results: list[ScenarioResult]
    warnings: list[str]

    def to_facts(self) -> dict[str, object]:
        return {
            "tariff_id": self.baseline_bill.tariff_id,
            "tariff_version": self.baseline_bill.tariff_version,
            "tariff_verified": self.baseline_bill.tariff_verified,
            "baseline": self.baseline_bill.to_facts(),
            "scenarios": [r.to_facts() for r in self.results],
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------- engine


def simulate(
    baseline: MonthlyUsage,
    scenarios: Sequence[Scenario],
    table: TariffTable,
) -> SimulationResult:
    """Price the baseline once, then each scenario against it. Raises `ScenarioError` for an inapplicable one."""
    base_bill = calculate_bill(baseline, table)
    names: dict[str, int] = {}
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        base_name = scenario.label or scenario.kind
        names[base_name] = names.get(base_name, 0) + 1
        name = base_name if names[base_name] == 1 else f"{base_name}_{names[base_name]}"
        results.append(_run(name, scenario, baseline, base_bill, table))
    warnings = list(dict.fromkeys(w for b in [base_bill, *(r.scenario_bill for r in results)] for w in b.warnings))
    return SimulationResult(baseline_bill=base_bill, results=results, warnings=warnings)


def _run(name: str, scenario: Scenario, baseline: MonthlyUsage, base_bill: BillBreakdown, table: TariffTable) -> ScenarioResult:
    new_table = table
    if isinstance(scenario, AddSolar):
        new_usage, assumptions = _add_solar(scenario, baseline)
    elif isinstance(scenario, AddBattery):
        new_usage, assumptions = _add_battery(scenario, baseline, table)
    elif isinstance(scenario, ShiftLoad):
        new_usage, assumptions = _shift_load(scenario, baseline, table)
    elif isinstance(scenario, ChangeTariff):
        new_usage, new_table = baseline, scenario.other_table
        assumptions = [f"Same usage priced under {new_table.id} v{new_table.version}; behaviour does not change."]
    else:
        new_usage, assumptions = _reduce(scenario, baseline)

    try:
        new_bill = calculate_bill(new_usage, new_table)
    except ValueError as exc:
        raise ScenarioError(f"{name}: {exc}") from exc

    assumptions = assumptions + [
        "One month is priced as a snapshot; per-year figures are exactly 12 times the monthly figure (no seasonality).",
    ]
    if new_bill.banked_units_out_kwh > 0:
        assumptions.append("Units banked at month end are reported but not valued in this month's saving.")
    if scenario.capex_inr is not None:
        assumptions.append("Payback = capex divided by the monthly saving; no subsidy, financing, degradation or tariff escalation.")

    delta = new_bill.total_inr - base_bill.total_inr
    saving = -delta
    capex = scenario.capex_inr
    payback = None
    if capex is not None and saving > 0:
        payback = (capex / saving).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return ScenarioResult(
        name=name,
        kind=scenario.kind,
        baseline_total_inr=base_bill.total_inr,
        scenario_total_inr=new_bill.total_inr,
        delta_import_kwh=new_bill.import_kwh - base_bill.import_kwh,
        delta_export_kwh=new_bill.export_kwh - base_bill.export_kwh,
        delta_inr_month=delta,
        delta_inr_year=delta * MONTHS_PER_YEAR,
        saving_inr_month=saving,
        saving_inr_year=saving * MONTHS_PER_YEAR,
        capex_inr=capex,
        payback_months=payback,
        direction="saving" if saving > 0 else "extra_cost" if saving < 0 else "no_change",
        assumptions=assumptions,
        scenario_bill=new_bill,
        warnings=list(new_bill.warnings),
    )


# --------------------------------------------------------------------------- usage transforms


def _rebuild(base: MonthlyUsage, imp: Decimal, exp: Decimal, bands: dict[str, Decimal] | None) -> MonthlyUsage:
    """New usage with the same period facts; import is re-derived from bands so the two never disagree."""
    if bands is not None:
        imp = sum(bands.values(), ZERO)
    return MonthlyUsage(
        import_kwh=imp, export_kwh=exp, import_by_band=bands, days=base.days,
        sanctioned_load_kw=base.sanctioned_load_kw, banked_units_in=base.banked_units_in,
        carried_credit_in_inr=base.carried_credit_in_inr,
    )


def _scale_bands(bands: dict[str, Decimal] | None, keep: Decimal) -> dict[str, Decimal] | None:
    return None if bands is None else {k: v * keep for k, v in bands.items()}


def _add_solar(s: AddSolar, base: MonthlyUsage) -> tuple[MonthlyUsage, list[str]]:
    generation = s.kwp * s.yield_kwh_per_kwp_month
    self_used = min(generation * s.self_consumption_share, base.import_kwh)
    exported = generation - self_used
    keep = ZERO if base.import_kwh == 0 else (base.import_kwh - self_used) / base.import_kwh
    usage = _rebuild(base, base.import_kwh - self_used, base.export_kwh + exported, _scale_bands(base.import_by_band, keep))
    notes = [
        f"Added PV generates {_n(generation)} kWh/month ({s.kwp} kWp x {s.yield_kwh_per_kwp_month} kWh per kWp per month).",
        f"{_n(s.self_consumption_share * 100, 0)}% of generation is assumed used on site, capped at the home's import; "
        f"{_n(self_used)} kWh offsets imports and {_n(exported)} kWh is exported.",
    ]
    if base.import_by_band is not None:
        notes.append("Offset import is removed pro rata across ToD bands (solar timing is not modelled).")
    return usage, notes


def _add_battery(s: AddBattery, base: MonthlyUsage, table: TariffTable) -> tuple[MonthlyUsage, list[str]]:
    throughput = s.capacity_kwh * s.usable_fraction * s.cycles_per_month
    eff = s.round_trip_efficiency
    notes = [
        f"Battery throughput = {s.capacity_kwh} kWh x {s.usable_fraction} usable x {s.cycles_per_month} cycles = {_n(throughput)} kWh/month, "
        f"round-trip efficiency {s.round_trip_efficiency}."
    ]
    if s.strategy == "absorb_exports":
        stored = min(throughput, base.export_kwh, base.import_kwh / eff)
        delivered = stored * eff
        keep = ZERO if base.import_kwh == 0 else (base.import_kwh - delivered) / base.import_kwh
        usage = _rebuild(base, base.import_kwh - delivered, base.export_kwh - stored, _scale_bands(base.import_by_band, keep))
        notes.append(f"Stores {_n(stored)} kWh of exports and returns {_n(delivered)} kWh later, replacing the same amount of import; losses are {_n(stored - delivered)} kWh.")
        if table.export.mode == "net_metering" and table.export.bank_units:
            notes.append("Under this net-metering tariff banked units already return 1:1, so a battery that absorbs exports can only lose value.")
        return usage, notes

    if base.import_by_band is None or not table.tod_bands:
        raise ScenarioError("shift_import needs a ToD tariff and usage split by ToD band")
    src, dst = s.from_band, s.to_band
    for band in (src, dst):
        if band not in table.band_names:
            raise ScenarioError(f"unknown ToD band {band!r}; tariff defines {table.band_names}")
    assert src and dst and s.shiftable_share is not None  # guaranteed by the model validator
    shifted = min(throughput, base.import_by_band.get(src, ZERO) * s.shiftable_share)
    bands = dict(base.import_by_band)
    bands[src] = bands.get(src, ZERO) - shifted
    bands[dst] = bands.get(dst, ZERO) + shifted / eff
    notes.append(
        f"At most {_n(s.shiftable_share * 100, 0)}% of {src}-band import can be covered; {_n(shifted)} kWh moves from {src} to {dst}, "
        f"drawing {_n(shifted / eff)} kWh in {dst} to cover losses."
    )
    return _rebuild(base, ZERO, base.export_kwh, bands), notes


def _shift_load(s: ShiftLoad, base: MonthlyUsage, table: TariffTable) -> tuple[MonthlyUsage, list[str]]:
    if base.import_by_band is None or not table.tod_bands:
        raise ScenarioError("shift_load needs a ToD tariff and usage split by ToD band")
    for band in (s.from_band, s.to_band):
        if band not in table.band_names:
            raise ScenarioError(f"unknown ToD band {band!r}; tariff defines {table.band_names}")
    moved = min(s.kwh, base.import_by_band.get(s.from_band, ZERO))
    bands = dict(base.import_by_band)
    bands[s.from_band] = bands.get(s.from_band, ZERO) - moved
    bands[s.to_band] = bands.get(s.to_band, ZERO) + moved
    notes = [f"Moves {_n(moved)} kWh from {s.from_band} to {s.to_band} with no loss (asked for {s.kwh}; capped at what the {s.from_band} band imports)."]
    return _rebuild(base, ZERO, base.export_kwh, bands), notes


def _reduce(s: ReduceConsumption, base: MonthlyUsage) -> tuple[MonthlyUsage, list[str]]:
    keep = (Decimal(100) - s.percent) / Decimal(100)
    bands = _scale_bands(base.import_by_band, keep)
    usage = _rebuild(base, base.import_kwh * keep, base.export_kwh, bands)
    return usage, [f"Import falls by {s.percent}% across all hours; exports are unchanged."]
