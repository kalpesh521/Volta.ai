"""
Typed inputs and outputs of the optimizer engine.

The engine is a pure function of an `OptimizationProblem`: it knows nothing about the database, the
forecast engine or the tariff engine. The agent layer maps telemetry, forecasts and tariffs onto these
frozen dataclasses and feeds `OptimizationResult.to_facts()` to the answer prompt.

WHY frozen + validated at construction: a problem with a negative price, a 120 % state of charge or an
appliance window outside the horizon would not crash the solver, it would produce a plausible plan that
is wrong. Every rule that can be checked on the way in IS checked on the way in
(`InvalidProblemError`), so bad data fails loudly where it entered instead of inside a chat answer.

Conventions
-----------
* Power is kW, energy kWh, money INR. A slot of `slot_minutes` carries `kW * slot_minutes / 60` kWh.
* Slot start times are expected in the HOME'S LOCAL time zone: labels in the facts ("11:00") are
  formatted straight from them, never converted.
* Appliance windows are slot INDEXES (0 = first slot of the horizon, `latest_end` exclusive). Mapping
  clock times to indexes is the caller's job (`OptimizationProblem.slot_index_at` helps).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

# Slot lengths the engine is tested with: they divide an hour exactly so `kW * dt` stays clean.
ALLOWED_SLOT_MINUTES = (5, 10, 15, 20, 30, 60)

# A load that cannot be served is never dropped silently. In the DP it is priced at this (absurd) rate so
# the solver avoids it whenever any alternative exists, while still returning a plan (with the shortfall
# reported) when it cannot be avoided, e.g. a long outage with a small battery.
UNSERVED_PENALTY_INR_PER_KWH = 1_000_000.0

# Fraction of usable capacity held back by `Goal.MAXIMIZE_BACKUP` when the caller gave no explicit
# `backup_reserve_kwh`. 0.8 = "keep four-fifths of the battery for the outage".
DEFAULT_BACKUP_GOAL_RESERVE_FRACTION = 0.8


class InvalidProblemError(ValueError):
    """The problem description is malformed (negative power, window outside the horizon, ...)."""


class Goal(str, Enum):
    """What "best" means. Only changes the objective weights and the battery reserve, never the hard rules."""

    REDUCE_BILL = "reduce_bill"
    MAXIMIZE_SELF_CONSUMPTION = "maximize_self_consumption"
    MAXIMIZE_BACKUP = "maximize_backup"


class Status(str, Enum):
    OPTIMAL = "optimal"  # exact battery engine (or exact on its grid) and at most one shiftable appliance
    FEASIBLE_HEURISTIC = "feasible_heuristic"  # verified, but several appliances: coordinate descent
    INFEASIBLE = "infeasible"  # no verified plan (appliance cannot be placed, or verifier objected)


@dataclass(frozen=True)
class GoalWeights:
    """
    Objective weights per goal, in INR-equivalent per kWh.

    `reduce_bill` is pure money. `maximize_self_consumption` adds a penalty on every kWh imported AND
    exported, which pushes the solver to store solar rather than export it and to discharge rather than
    import, even when the money difference is small. `maximize_backup` has no money penalty; it instead
    prices energy missing from the reserve and values energy left in the battery at the end.

    These are tuning knobs, not physics: they are INR-equivalents chosen so each goal behaves sensibly
    on Indian residential tariffs (roughly 4-12 INR/kWh), and they are echoed in `result.meta`.
    """

    self_consumption_import_penalty: float = 4.0
    self_consumption_export_penalty: float = 4.0
    backup_shortfall_penalty_per_kwh_hour: float = 5.0
    backup_stored_energy_value: float = 2.0


@dataclass(frozen=True, kw_only=True)
class SlotInput:
    start: datetime
    solar_kw: float
    base_load_kw: float
    import_price_inr_per_kwh: float
    export_credit_inr_per_kwh: float = 0.0
    grid_available: bool = True

    def __post_init__(self) -> None:
        _check(self.solar_kw, "solar_kw")
        _check(self.base_load_kw, "base_load_kw")
        _check(self.import_price_inr_per_kwh, "import_price_inr_per_kwh")
        _check(self.export_credit_inr_per_kwh, "export_credit_inr_per_kwh")


@dataclass(frozen=True, kw_only=True)
class BatterySpec:
    usable_capacity_kwh: float
    current_soc_percent: float
    max_charge_kw: float
    max_discharge_kw: float
    min_soc_percent: float = 10.0
    round_trip_efficiency: float = 0.9
    backup_reserve_kwh: float = 0.0

    def __post_init__(self) -> None:
        _check(self.usable_capacity_kwh, "usable_capacity_kwh")
        _check(self.current_soc_percent, "current_soc_percent", hi=100.0)
        _check(self.min_soc_percent, "min_soc_percent", hi=100.0)
        _check(self.max_charge_kw, "max_charge_kw")
        _check(self.max_discharge_kw, "max_discharge_kw")
        _check(self.backup_reserve_kwh, "backup_reserve_kwh")
        if not (0.0 < self.round_trip_efficiency <= 1.0):
            raise InvalidProblemError("round_trip_efficiency must be in (0, 1]")


@dataclass(frozen=True, kw_only=True)
class GridSpec:
    sanctioned_load_kw: float
    zero_export: bool = False
    max_export_kw: float | None = None
    inverter_capacity_kw: float | None = None

    def __post_init__(self) -> None:
        _check(self.sanctioned_load_kw, "sanctioned_load_kw")
        if self.max_export_kw is not None:
            _check(self.max_export_kw, "max_export_kw")
        if self.inverter_capacity_kw is not None:
            _check(self.inverter_capacity_kw, "inverter_capacity_kw")


@dataclass(frozen=True, kw_only=True)
class ApplianceSpec:
    """
    A load of `rated_kw` that runs `duration_slots` consecutive slots, inside [earliest_start, latest_end).

    `preferred_start` is when the household would run it anyway; it defines the baseline and is the
    tie-break (the solver only moves an appliance when the saving is worth the inconvenience).
    `critical` loads (a medical device, a fridge compressor modelled as a block) are NEVER shifted: they
    run at `preferred_start` and are treated as fixed load. `must_run=False` means "nice to have": if no
    placement is feasible it is skipped with a warning instead of making the whole plan infeasible.
    """

    name: str
    rated_kw: float
    duration_slots: int
    earliest_start: int = 0
    latest_end: int | None = None  # exclusive; None = end of horizon
    preferred_start: int | None = None  # None = earliest_start
    must_run: bool = True
    critical: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidProblemError("appliance name must not be empty")
        _check(self.rated_kw, f"{self.name}.rated_kw")
        if self.duration_slots < 1:
            raise InvalidProblemError(f"{self.name}: duration_slots must be >= 1")
        if self.earliest_start < 0:
            raise InvalidProblemError(f"{self.name}: earliest_start must be >= 0")

    def window_end(self, n_slots: int) -> int:
        return n_slots if self.latest_end is None else self.latest_end

    def preferred(self) -> int:
        return self.earliest_start if self.preferred_start is None else self.preferred_start

    def feasible_starts(self, n_slots: int) -> range:
        """Start indexes whose whole run fits inside the window (and the horizon)."""
        last = min(self.window_end(n_slots), n_slots) - self.duration_slots
        return range(self.earliest_start, last + 1)


@dataclass(frozen=True, kw_only=True)
class OptimizationProblem:
    slots: tuple[SlotInput, ...]
    grid: GridSpec
    battery: BatterySpec | None = None
    appliances: tuple[ApplianceSpec, ...] = ()
    goal: Goal = Goal.REDUCE_BILL
    slot_minutes: int = 60
    # Opt-in: charging from the grid is a decision about the household's tariff and battery wear, so the
    # default is the safe one (solar surplus only). Arbitrage plans need it switched on.
    allow_grid_charging: bool = False
    # "exact" (convex piecewise-linear DP, default) or "grid" (SOC-grid DP, see engine.py). A battery that
    # starts below its reserve always uses the grid engine.
    battery_engine: str = "exact"
    soc_step_percent: float = 2.0  # grid engine only
    terminal_value_inr_per_kwh: float | None = None  # None = goal default (0, or backup value)
    min_shift_saving_inr: float = 0.5  # do not inconvenience the household for less than this
    max_descent_rounds: int = 4
    weights: GoalWeights = field(default_factory=GoalWeights)

    def __post_init__(self) -> None:
        if not self.slots:
            raise InvalidProblemError("at least one slot is required")
        if self.slot_minutes not in ALLOWED_SLOT_MINUTES:
            raise InvalidProblemError(f"slot_minutes must be one of {ALLOWED_SLOT_MINUTES}")
        if not (0.1 <= self.soc_step_percent <= 25.0):
            raise InvalidProblemError("soc_step_percent must be in [0.1, 25]")
        if self.battery_engine not in ("exact", "grid"):
            raise InvalidProblemError("battery_engine must be 'exact' or 'grid'")
        if self.max_descent_rounds < 1:
            raise InvalidProblemError("max_descent_rounds must be >= 1")
        _check(self.min_shift_saving_inr, "min_shift_saving_inr")
        step = timedelta(minutes=self.slot_minutes)
        for earlier, later in zip(self.slots, self.slots[1:]):
            if later.start - earlier.start != step:
                raise InvalidProblemError(f"slots must be contiguous, {self.slot_minutes} minutes apart")
        names: set[str] = set()
        n = len(self.slots)
        for appliance in self.appliances:
            if appliance.name in names:
                raise InvalidProblemError(f"duplicate appliance name: {appliance.name}")
            names.add(appliance.name)
            _validate_window(appliance, n)

    @property
    def n_slots(self) -> int:
        return len(self.slots)

    @property
    def dt_hours(self) -> float:
        return self.slot_minutes / 60.0

    def slot_index_at(self, when: datetime) -> int | None:
        """Index of the slot that contains `when`, or None if outside the horizon."""
        first = self.slots[0].start
        if when < first:
            return None
        index = int((when - first) / timedelta(minutes=self.slot_minutes))
        return index if index < self.n_slots else None


def _validate_window(appliance: ApplianceSpec, n: int) -> None:
    end = appliance.window_end(n)
    if end > n or end <= appliance.earliest_start:
        raise InvalidProblemError(f"{appliance.name}: window [{appliance.earliest_start}, {end}) is outside the horizon")
    starts = appliance.feasible_starts(n)
    if len(starts) == 0:
        raise InvalidProblemError(f"{appliance.name}: duration does not fit in its window")
    if appliance.preferred() not in starts:
        raise InvalidProblemError(f"{appliance.name}: preferred_start is outside its window")


def _check(value: float, name: str, *, hi: float | None = None) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise InvalidProblemError(f"{name} must be a finite number >= 0, got {value!r}")
    if hi is not None and value > hi:
        raise InvalidProblemError(f"{name} must be <= {hi}, got {value!r}")


# --------------------------------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class Violation:
    """One broken hard constraint, found by the independent verifier (or an unplaceable appliance)."""

    code: str
    message: str
    slot: int | None = None
    magnitude: float = 0.0


@dataclass(frozen=True)
class SlotPlan:
    index: int
    start: datetime
    grid_available: bool
    solar_kw: float
    base_load_kw: float
    appliance_kw: float
    load_kw: float  # base + appliances (what the household wants)
    solar_used_kw: float  # solar actually used (solar - curtailed)
    curtailed_kw: float
    battery_charge_kw: float  # drawn from the system (solar/grid side)
    battery_discharge_kw: float  # delivered to the system
    grid_import_kw: float
    grid_export_kw: float
    unserved_kw: float
    soc_kwh: float  # end of slot
    soc_percent: float
    import_price_inr_per_kwh: float
    export_credit_inr_per_kwh: float
    cost_inr: float  # import cost minus export credit for this slot (can be negative)
    action: str  # "charge" | "discharge" | "idle"


@dataclass(frozen=True)
class ApplianceSchedule:
    name: str
    rated_kw: float
    duration_slots: int
    critical: bool
    must_run: bool
    placed: bool
    start_slot: int | None
    start: datetime | None
    end: datetime | None
    preferred_start_slot: int
    preferred_start: datetime
    shifted: bool
    # Marginal saving vs running at the preferred time, everything else as planned. None when the
    # preferred time is itself infeasible (exceeds the sanctioned load) or the appliance is not placed.
    saves_inr: float | None
    preferred_feasible: bool


@dataclass(frozen=True)
class PlanTotals:
    cost_inr: float  # import cost minus export credit (grid bill for the horizon)
    grid_import_kwh: float
    grid_export_kwh: float
    solar_kwh: float
    solar_used_kwh: float
    curtailed_kwh: float
    load_kwh: float
    unserved_kwh: float
    battery_charged_kwh: float
    battery_discharged_kwh: float
    self_consumption_percent: float  # solar used on site / solar generated
    self_sufficiency_percent: float  # load met without grid import
    final_soc_percent: float | None


@dataclass(frozen=True)
class OptimizationResult:
    status: Status
    goal: Goal
    slot_minutes: int
    slots: tuple[SlotPlan, ...]
    appliances: tuple[ApplianceSchedule, ...]
    totals: PlanTotals | None
    baseline: PlanTotals | None
    savings_inr: float
    savings_kwh: float  # grid import avoided vs baseline
    savings_percent: float
    violations: tuple[Violation, ...] = ()
    warnings: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not Status.INFEASIBLE and not self.violations

    def to_dict(self) -> dict[str, Any]:
        """Plain, `json.dumps`-able structure (datetimes as ISO strings, enums as values)."""
        return _jsonable(self)

    def to_facts(self) -> dict[str, Any]:
        """Compact, rounded, labelled facts for the answer model. See `facts.build_facts`."""
        from .facts import build_facts

        return build_facts(self)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class PlanVerificationError(RuntimeError):
    """Raised by `optimize(..., strict=True)` when the verifier finds a violation in the solver's own plan."""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        self.violations = violations
        shown = "; ".join(f"{v.code}: {v.message}" for v in violations[:3])
        super().__init__(f"{len(violations)} constraint violation(s): {shown}")
