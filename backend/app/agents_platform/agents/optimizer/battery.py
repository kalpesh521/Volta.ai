"""
Battery model, per-slot energy flows, the default (baseline) policy, plan execution and scoring.

Everything here is independent of HOW the battery schedule is found (`convex_dp.py` exact engine,
`grid_dp.py` SOC-grid engine); both import the same physics so they cannot disagree about the rules.

The model (all quantities per slot, `dt` hours long)
---------------------------------------------------
    load      = base load + appliances running in the slot            (what the household wants)
    solar_eff = min(solar, inverter capacity)                         (the rest is curtailed)
    net       = load - solar_eff - discharge + charge
    net >= 0 : import = min(net, import cap), the rest is UNSERVED    (never silently dropped)
    net <  0 : export = min(-net, export cap), the rest is CURTAILED  (zero-export / outage => 0)

`charge` is what the battery draws from the system, `discharge` is what it delivers. Efficiency is applied
symmetrically: eta_one_way = sqrt(round_trip). Stored energy changes by `charge*dt*eta - discharge*dt/eta`.
The battery never exports: discharge only serves on-site load (net-metering rules differ per state and
battery-to-grid export is not something a home assistant should do by default). It never charges and
discharges in the same slot.

Objective vs. money
-------------------
The solver minimises an INR-equivalent OBJECTIVE; the plan's reported cost is always the true grid bill.
They differ only through goal weights and one convexity guard: the value of exporting a kWh in the
objective is `min(max(credit - export_penalty, 0), import_price + import_penalty)`. A credit above the
import price would make cycling through the grid look profitable (it is not allowed anyway) and an export
penalty above the credit would make curtailing better than exporting; clamping keeps the per-slot cost
convex, which is what lets the exact engine be exact.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

from .models import (
    DEFAULT_BACKUP_GOAL_RESERVE_FRACTION,
    UNSERVED_PENALTY_INR_PER_KWH,
    Goal,
    OptimizationProblem,
)

INF = math.inf
_EPS = 1e-9
_TIE = 1e-9  # costs closer than this are a tie; ties resolve to the smaller battery move


class Flows(NamedTuple):
    grid_import: float
    grid_export: float
    curtailed: float
    unserved: float


@dataclass(frozen=True)
class SlotEnv:
    """Everything about a slot that does not depend on the decision."""

    solar_kw: float
    solar_eff_kw: float
    price: float
    credit: float
    import_cap_kw: float
    export_cap_kw: float
    charge_cap_kw: float  # battery limit and inverter limit
    discharge_cap_kw: float  # battery limit and inverter headroom left by solar
    hold_for_outage: bool  # maximize_backup, grid up: battery discharges only to stay within sanctioned load


@dataclass(frozen=True)
class BatteryParams:
    capacity_kwh: float
    e0_kwh: float
    floor_kwh: float  # reserve: never discharged below
    eta: float  # one-way efficiency
    max_charge_kw: float
    max_discharge_kw: float


@dataclass(frozen=True)
class EffectiveWeights:
    import_penalty: float
    export_penalty: float
    shortfall_penalty: float  # INR per kWh-below-reserve per hour
    terminal_value: float  # INR per kWh stored at the end of the horizon


@dataclass(frozen=True)
class Context:
    """Everything the DP, the baseline and the executor share. Built once per problem."""

    dt: float
    envs: tuple[SlotEnv, ...]
    params: BatteryParams | None
    weights: EffectiveWeights
    allow_grid_charging: bool
    warnings: tuple[str, ...]


def build_context(problem: OptimizationProblem) -> Context:
    dt = problem.dt_hours
    grid = problem.grid
    inverter = grid.inverter_capacity_kw
    battery = problem.battery
    warnings: list[str] = []

    params: BatteryParams | None = None
    if battery is not None and battery.usable_capacity_kwh > 0:
        cap = battery.usable_capacity_kwh
        reserve = battery.backup_reserve_kwh
        if problem.goal is Goal.MAXIMIZE_BACKUP and reserve <= 0:
            reserve = DEFAULT_BACKUP_GOAL_RESERVE_FRACTION * cap
        if reserve > cap:
            warnings.append("Backup reserve is larger than the battery; holding the battery full.")
        floor = min(cap, max(battery.min_soc_percent / 100.0 * cap, reserve))
        e0 = battery.current_soc_percent / 100.0 * cap
        if e0 < floor - _EPS:
            warnings.append("Battery is currently below its reserve; it is not discharged until it has recovered above it.")
        params = BatteryParams(
            capacity_kwh=cap,
            e0_kwh=e0,
            floor_kwh=floor,
            eta=math.sqrt(battery.round_trip_efficiency),
            max_charge_kw=min(battery.max_charge_kw, inverter) if inverter is not None else battery.max_charge_kw,
            max_discharge_kw=battery.max_discharge_kw,
        )

    envs: list[SlotEnv] = []
    for slot in problem.slots:
        solar_eff = min(slot.solar_kw, inverter) if inverter is not None else slot.solar_kw
        if not slot.grid_available or grid.zero_export:
            export_cap = 0.0
        else:
            export_cap = INF if grid.max_export_kw is None else grid.max_export_kw
        if params is None:
            charge_cap = discharge_cap = 0.0
        else:
            charge_cap = params.max_charge_kw
            discharge_cap = params.max_discharge_kw
            if inverter is not None:
                discharge_cap = min(discharge_cap, max(0.0, inverter - solar_eff))
        envs.append(
            SlotEnv(
                solar_kw=slot.solar_kw,
                solar_eff_kw=solar_eff,
                price=slot.import_price_inr_per_kwh,
                credit=slot.export_credit_inr_per_kwh,
                import_cap_kw=grid.sanctioned_load_kw if slot.grid_available else 0.0,
                export_cap_kw=export_cap,
                charge_cap_kw=charge_cap,
                discharge_cap_kw=discharge_cap,
                hold_for_outage=problem.goal is Goal.MAXIMIZE_BACKUP and slot.grid_available,
            )
        )
    return Context(dt, tuple(envs), params, _effective_weights(problem), problem.allow_grid_charging, tuple(warnings))


def _effective_weights(problem: OptimizationProblem) -> EffectiveWeights:
    w = problem.weights
    goal = problem.goal
    import_pen = w.self_consumption_import_penalty if goal is Goal.MAXIMIZE_SELF_CONSUMPTION else 0.0
    export_pen = w.self_consumption_export_penalty if goal is Goal.MAXIMIZE_SELF_CONSUMPTION else 0.0
    shortfall = w.backup_shortfall_penalty_per_kwh_hour if goal is Goal.MAXIMIZE_BACKUP else 0.0
    if problem.terminal_value_inr_per_kwh is not None:
        terminal = problem.terminal_value_inr_per_kwh
    else:
        terminal = w.backup_stored_energy_value if goal is Goal.MAXIMIZE_BACKUP else 0.0
    return EffectiveWeights(import_pen, export_pen, shortfall, terminal)


# ------------------------------------------------------------------------------------- slot physics


def resolve_flows(env: SlotEnv, load_kw: float, charge_kw: float, discharge_kw: float) -> Flows:
    """Grid import/export, curtailment and unserved load for one slot given the battery decision."""
    net = load_kw - env.solar_eff_kw - discharge_kw + charge_kw
    forced_curtail = env.solar_kw - env.solar_eff_kw
    if abs(net) < 1e-12:
        net = 0.0
    if net >= 0.0:
        grid_import = min(net, env.import_cap_kw)
        return Flows(grid_import, 0.0, forced_curtail, net - grid_import)
    surplus = -net
    grid_export = min(surplus, env.export_cap_kw)
    return Flows(0.0, grid_export, forced_curtail + (surplus - grid_export), 0.0)


def charge_available(env: SlotEnv, load_kw: float, allow_grid_charging: bool) -> float:
    """Largest charge power the supply side allows: never charge into an unserved load."""
    surplus = max(0.0, env.solar_eff_kw - load_kw)
    supply_limit = max(0.0, env.import_cap_kw + env.solar_eff_kw - load_kw)
    limit = min(env.charge_cap_kw, supply_limit)
    return limit if allow_grid_charging else min(limit, surplus)


def discharge_available(env: SlotEnv, load_kw: float, *, respect_hold: bool = True) -> float:
    """
    Largest discharge power: the battery serves on-site load only, within its and the inverter's limit.
    Under `maximize_backup` (`hold_for_outage`) on a grid-up slot it may only cover what the sanctioned
    import cannot: that is the "no arbitrage cycling" rule, a hard rule the verifier re-checks.
    """
    deficit = load_kw - env.solar_eff_kw
    if respect_hold and env.hold_for_outage:
        deficit -= env.import_cap_kw
    return max(0.0, min(env.discharge_cap_kw, deficit))


def slot_objective(env: SlotEnv, flows: Flows, dt: float, w: EffectiveWeights) -> float:
    """Grid money plus goal penalties plus the (huge) unserved penalty for one slot. See module docstring."""
    import_value = env.price + w.import_penalty
    export_value = min(max(env.credit - w.export_penalty, 0.0), import_value)
    return dt * (
        flows.grid_import * import_value
        - flows.grid_export * export_value
        + UNSERVED_PENALTY_INR_PER_KWH * flows.unserved
    )


# ------------------------------------------------------------------------------ execution + baseline


@dataclass(frozen=True)
class SlotExec:
    load_kw: float
    charge_kw: float
    discharge_kw: float
    flows: Flows
    soc_kwh: float  # end of slot


def baseline_intents(ctx: Context, loads: list[float]) -> list[float]:
    """Default behaviour of a hybrid inverter: store solar surplus, cover deficits, never grid-charge."""
    out: list[float] = []
    for env, load in zip(ctx.envs, loads):
        surplus = env.solar_eff_kw - load
        if ctx.params is None:
            out.append(0.0)
        elif surplus > 0:
            out.append(min(env.charge_cap_kw, surplus))
        else:
            out.append(-discharge_available(env, load, respect_hold=False))
    return out


def execute(
    ctx: Context, loads: list[float], intents: list[float], *, allow_grid_charging: bool, respect_hold: bool
) -> list[SlotExec]:
    """
    Run signed commands against the TRUE battery state. Every clamp here is a rule the verifier re-checks:
    headroom to 100 %, energy above the reserve, power limits and supply (no charging into unserved load).
    A command that cannot be fully honoured is honoured as far as the rules allow.
    """
    params = ctx.params
    dt = ctx.dt
    energy = params.e0_kwh if params is not None else 0.0
    out: list[SlotExec] = []
    for env, load, cmd in zip(ctx.envs, loads, intents):
        charge = discharge = 0.0
        if params is not None:
            if cmd > 0:
                charge = min(cmd, charge_available(env, load, allow_grid_charging))
                charge = max(0.0, min(charge, (params.capacity_kwh - energy) / (params.eta * dt)))
            elif cmd < 0:
                above_floor = max(0.0, energy - params.floor_kwh)
                discharge = min(-cmd, discharge_available(env, load, respect_hold=respect_hold), above_floor * params.eta / dt)
            energy += charge * dt * params.eta - discharge * dt / params.eta
        out.append(SlotExec(load, charge, discharge, resolve_flows(env, load, charge, discharge), energy))
    return out


def score(ctx: Context, execs: list[SlotExec]) -> float:
    """Objective of an executed plan: same terms as the DP, evaluated on the true trajectory."""
    w = ctx.weights
    total = 0.0
    params = ctx.params
    for env, ex in zip(ctx.envs, execs):
        total += slot_objective(env, ex.flows, ctx.dt, w)
        if params is not None and w.shortfall_penalty > 0:
            total += w.shortfall_penalty * ctx.dt * max(0.0, params.floor_kwh - ex.soc_kwh)
    if params is not None and execs:
        total -= w.terminal_value * execs[-1].soc_kwh
    return total
