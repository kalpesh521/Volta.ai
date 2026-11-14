"""
Independent verifier: recompute every hard constraint of a plan from scratch.

WHY a second implementation and not a reuse of the solver's own flow function: a checker that shares code
with the thing it checks shares its bugs. This module imports only the INPUT/OUTPUT dataclasses; the
physics (energy balance, efficiency, reserve floor, export and outage rules, inverter limit) is written
again here, straight from the rules in `battery.py`'s docstring, in a deliberately plain style. The
solver calls `verify_plan` on its own output and refuses to return a plan with a violation, so
"constraint violations = 0" is enforced by code, not asserted by a test alone.

What is checked, per slot unless noted
--------------------------------------
* appliances (whole plan): known name, placed if `must_run`, run fits [earliest, latest_end), duration and
  rating match the spec, critical loads at their preferred start, slot loads equal base + appliances
* battery: charge/discharge >= 0 and not both, power limits (battery and inverter), SOC trajectory
  recomputed with round-trip efficiency, SOC <= capacity, SOC never driven below the reserve floor
  (soc_after >= min(soc_before, floor)), discharge only serves on-site load, grid charging only when
  allowed, no on-grid discharge beyond sanctioned-load relief under `maximize_backup`, no battery => no battery flows
* energy balance: solar_used + discharge + import = (load - unserved) + charge + export
* grid: 0 <= import <= sanctioned load, outage => no import and no export, zero export => no export,
  export <= max export, never importing and exporting at once, unserved load only when import is maxed out
* totals: cost, import, export, unserved, solar used, battery energy and final SOC match a recomputation
* baseline: re-simulated here (self-consumption default, appliances at preferred start) and compared

Tolerance is 1e-6 (kW/kWh/INR): floating-point noise, never a real constraint slack.
"""
from __future__ import annotations

import math

from .models import (
    DEFAULT_BACKUP_GOAL_RESERVE_FRACTION,
    ApplianceSpec,
    Goal,
    OptimizationProblem,
    OptimizationResult,
    PlanTotals,
    Violation,
)

TOL = 1e-6


def verify_plan(problem: OptimizationProblem, result: OptimizationResult) -> list[Violation]:
    """All violations of `result` against `problem`. An empty list means the plan is valid."""
    found: list[Violation] = []

    def add(code: str, message: str, slot: int | None = None, magnitude: float = 0.0) -> None:
        found.append(Violation(code=code, message=message, slot=slot, magnitude=float(magnitude)))

    n = problem.n_slots
    dt = problem.dt_hours
    if len(result.slots) != n:
        add("slot_count", f"plan has {len(result.slots)} slots, problem has {n}")
        return found

    appliance_kw = _check_appliances(problem, result, add)
    battery = problem.battery
    grid = problem.grid
    inverter = grid.inverter_capacity_kw
    cap = battery.usable_capacity_kwh if battery else 0.0
    eta = math.sqrt(battery.round_trip_efficiency) if battery else 1.0
    floor = _reserve_floor(problem)
    energy = battery.current_soc_percent / 100.0 * cap if battery else 0.0
    backup_goal = problem.goal is Goal.MAXIMIZE_BACKUP

    sums = {k: 0.0 for k in ("cost", "imp", "exp", "unserved", "solar", "used", "curtailed", "load", "chg", "dis")}
    for t, (slot, plan) in enumerate(zip(problem.slots, result.slots)):
        if plan.index != t or plan.start != slot.start:
            add("slot_identity", "slot index/time does not match the problem", t)
        if abs(plan.solar_kw - slot.solar_kw) > TOL or abs(plan.base_load_kw - slot.base_load_kw) > TOL:
            add("input_mismatch", "plan solar/base load differs from the problem input", t)
        if plan.grid_available != slot.grid_available:
            add("input_mismatch", "plan grid availability differs from the problem input", t)

        load = slot.base_load_kw + appliance_kw[t]
        if abs(plan.appliance_kw - appliance_kw[t]) > TOL or abs(plan.load_kw - load) > TOL:
            add("load_mismatch", f"load {plan.load_kw:.4f} kW != base + appliances {load:.4f} kW", t, plan.load_kw - load)

        charge, discharge = plan.battery_charge_kw, plan.battery_discharge_kw
        imp, exp, unserved = plan.grid_import_kw, plan.grid_export_kw, plan.unserved_kw
        curtailed = plan.curtailed_kw
        solar_used = slot.solar_kw - curtailed

        # ---- battery
        if charge < -TOL or discharge < -TOL:
            add("negative_battery_power", "battery power must be >= 0", t, min(charge, discharge))
        if min(charge, discharge) > TOL:
            add("simultaneous_charge_discharge", "battery charges and discharges in the same slot", t, min(charge, discharge))
        if battery is None or cap <= 0:
            if charge > TOL or discharge > TOL:
                add("battery_flow_without_battery", "battery power used but there is no battery", t, max(charge, discharge))
        else:
            if charge > battery.max_charge_kw + TOL:
                add("charge_limit", f"charge {charge:.4f} kW > limit {battery.max_charge_kw:.4f} kW", t, charge - battery.max_charge_kw)
            if discharge > battery.max_discharge_kw + TOL:
                add("discharge_limit", f"discharge {discharge:.4f} kW > limit {battery.max_discharge_kw:.4f} kW", t, discharge - battery.max_discharge_kw)
            if inverter is not None and charge > inverter + TOL:
                add("inverter_limit", "battery charge exceeds inverter capacity", t, charge - inverter)
            before = energy
            energy = before + charge * dt * eta - discharge * dt / eta
            if abs(energy - plan.soc_kwh) > 1e-5:
                add("soc_mismatch", f"reported SOC {plan.soc_kwh:.5f} kWh != recomputed {energy:.5f} kWh", t, plan.soc_kwh - energy)
            if energy > cap + TOL:
                add("soc_above_capacity", f"SOC {energy:.4f} kWh > capacity {cap:.4f} kWh", t, energy - cap)
            if energy < min(before, floor) - TOL:
                add("soc_below_reserve", f"SOC {energy:.4f} kWh below reserve {floor:.4f} kWh", t, min(before, floor) - energy)
            if discharge > max(0.0, load - solar_used) + TOL:
                add("battery_export", "battery discharges more than the on-site load needs", t)
            grid_charge = charge - max(0.0, solar_used - (load - unserved))
            if grid_charge > TOL and not problem.allow_grid_charging:
                add("grid_charging_not_allowed", "battery charged from the grid but allow_grid_charging is off", t, grid_charge)
            if backup_goal and slot.grid_available and discharge > max(0.0, load - solar_used - grid.sanctioned_load_kw) + TOL:
                add("backup_cycling", "battery discharged on-grid beyond sanctioned-load relief under maximize_backup", t, discharge)

        # ---- solar / inverter
        if curtailed < -TOL or curtailed > slot.solar_kw + TOL:
            add("curtailment_range", "curtailed solar outside [0, solar]", t, curtailed)
        if inverter is not None and solar_used + discharge > inverter + TOL:
            add("inverter_limit", "solar used + discharge exceeds inverter capacity", t, solar_used + discharge - inverter)

        # ---- energy balance
        supply = solar_used + discharge + imp
        demand = (load - unserved) + charge + exp
        if abs(supply - demand) > TOL:
            add("energy_balance", f"supply {supply:.5f} kW != demand {demand:.5f} kW", t, supply - demand)

        # ---- grid rules
        cap_in = grid.sanctioned_load_kw if slot.grid_available else 0.0
        if imp < -TOL or exp < -TOL:
            add("negative_grid_flow", "grid import/export must be >= 0", t, min(imp, exp))
        if imp > grid.sanctioned_load_kw + TOL:
            add("sanctioned_load", f"import {imp:.4f} kW > sanctioned load {grid.sanctioned_load_kw:.4f} kW", t, imp - grid.sanctioned_load_kw)
        if not slot.grid_available and (imp > TOL or exp > TOL):
            add("outage_grid_flow", "grid import/export during an outage", t, max(imp, exp))
        if grid.zero_export and exp > TOL:
            add("zero_export", f"export {exp:.4f} kW with zero-export on", t, exp)
        if grid.max_export_kw is not None and exp > grid.max_export_kw + TOL:
            add("export_limit", f"export {exp:.4f} kW > limit {grid.max_export_kw:.4f} kW", t, exp - grid.max_export_kw)
        if min(imp, exp) > TOL:
            add("simultaneous_import_export", "importing and exporting in the same slot", t, min(imp, exp))
        if unserved < -TOL or unserved > load + TOL:
            add("unserved_range", "unserved load outside [0, load]", t, unserved)
        if unserved > TOL and imp < cap_in - TOL:
            add("unserved_with_headroom", "load left unserved while grid import headroom remained", t, unserved)

        dt_cost = dt * (imp * slot.import_price_inr_per_kwh - exp * slot.export_credit_inr_per_kwh)
        if abs(plan.cost_inr - dt_cost) > TOL:
            add("cost_mismatch", f"slot cost {plan.cost_inr:.5f} != recomputed {dt_cost:.5f}", t, plan.cost_inr - dt_cost)

        sums["cost"] += dt_cost
        sums["imp"] += imp * dt
        sums["exp"] += exp * dt
        sums["unserved"] += unserved * dt
        sums["solar"] += slot.solar_kw * dt
        sums["used"] += solar_used * dt
        sums["curtailed"] += curtailed * dt
        sums["load"] += load * dt
        sums["chg"] += charge * dt
        sums["dis"] += discharge * dt

    _check_totals(result.totals, sums, energy, cap, add)
    _check_baseline(problem, result, add)
    return found


def _check_appliances(problem: OptimizationProblem, result: OptimizationResult, add) -> list[float]:
    n = problem.n_slots
    specs = {a.name: a for a in problem.appliances}
    scheduled = {s.name: s for s in result.appliances}
    kw = [0.0] * n
    for name in scheduled:
        if name not in specs:
            add("unknown_appliance", f"scheduled appliance {name!r} is not in the problem")
    for spec in problem.appliances:
        item = scheduled.get(spec.name)
        if item is None or not item.placed or item.start_slot is None:
            if spec.must_run or spec.critical:
                add("appliance_not_placed", f"{spec.name} must run but has no start time")
            continue
        start = item.start_slot
        end = start + spec.duration_slots
        if start < spec.earliest_start or end > min(spec.window_end(n), n):
            add("appliance_window", f"{spec.name} runs slots {start}-{end - 1}, outside its window")
            continue
        if item.duration_slots != spec.duration_slots or abs(item.rated_kw - spec.rated_kw) > TOL:
            add("appliance_spec_mismatch", f"{spec.name} duration/rating differs from the request")
        if spec.critical and start != spec.preferred():
            add("critical_shifted", f"critical load {spec.name} was moved off its preferred start")
        if item.start != problem.slots[start].start:
            add("appliance_time_mismatch", f"{spec.name} start time does not match its slot")
        for t in range(start, end):
            kw[t] += spec.rated_kw
    return kw


def _reserve_floor(problem: OptimizationProblem) -> float:
    battery = problem.battery
    if battery is None:
        return 0.0
    cap = battery.usable_capacity_kwh
    reserve = battery.backup_reserve_kwh
    if problem.goal is Goal.MAXIMIZE_BACKUP and reserve <= 0:
        reserve = DEFAULT_BACKUP_GOAL_RESERVE_FRACTION * cap
    return min(cap, max(battery.min_soc_percent / 100.0 * cap, reserve))


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-5 * (1.0 + abs(b))


def _check_totals(totals: PlanTotals | None, sums: dict[str, float], energy: float, cap: float, add) -> None:
    if totals is None:
        add("totals_missing", "plan has no totals")
        return
    expected = {
        "cost_inr": sums["cost"],
        "grid_import_kwh": sums["imp"],
        "grid_export_kwh": sums["exp"],
        "unserved_kwh": sums["unserved"],
        "solar_kwh": sums["solar"],
        "solar_used_kwh": sums["used"],
        "curtailed_kwh": sums["curtailed"],
        "load_kwh": sums["load"],
        "battery_charged_kwh": sums["chg"],
        "battery_discharged_kwh": sums["dis"],
    }
    for name, value in expected.items():
        if not _close(getattr(totals, name), value):
            add("totals_mismatch", f"{name}: reported {getattr(totals, name):.5f}, recomputed {value:.5f}")
    if cap > 0 and (totals.final_soc_percent is None or not _close(totals.final_soc_percent, energy / cap * 100.0)):
        add("totals_mismatch", "final_soc_percent does not match the recomputed trajectory")
    onsite = max(0.0, sums["used"] - sums["exp"])
    pct = onsite / sums["solar"] * 100.0 if sums["solar"] > 0 else 0.0
    if not _close(totals.self_consumption_percent, min(100.0, pct)):
        add("totals_mismatch", f"self_consumption_percent: reported {totals.self_consumption_percent:.4f}, recomputed {pct:.4f}")


def _check_baseline(problem: OptimizationProblem, result: OptimizationResult, add) -> None:
    if result.baseline is None:
        add("baseline_missing", "plan has no baseline")
        return
    placed = {s.name for s in result.appliances if s.placed}
    mine = baseline_totals(problem, placed)
    for name in ("cost_inr", "grid_import_kwh", "grid_export_kwh", "unserved_kwh"):
        if not _close(getattr(result.baseline, name), mine[name]):
            add("baseline_mismatch", f"baseline {name}: reported {getattr(result.baseline, name):.5f}, recomputed {mine[name]:.5f}")
    if mine["final_soc_percent"] is not None and (
        result.baseline.final_soc_percent is None or not _close(result.baseline.final_soc_percent, mine["final_soc_percent"])
    ):
        add("baseline_mismatch", "baseline final SOC differs from the recomputation")


def baseline_totals(problem: OptimizationProblem, placed_names: set[str] | None = None) -> dict[str, float | None]:
    """
    Re-simulate the NO-OPTIMISATION default: appliances at their preferred start, battery in plain
    self-consumption mode (store solar surplus, cover deficits, never charge from the grid, never go below
    the reserve). `placed_names` limits which appliances run (default: all), so plan and baseline always
    run the same set of appliances.
    """
    n, dt = problem.n_slots, problem.dt_hours
    grid, battery = problem.grid, problem.battery
    inverter = grid.inverter_capacity_kw
    cap = battery.usable_capacity_kwh if battery else 0.0
    eta = math.sqrt(battery.round_trip_efficiency) if battery else 1.0
    floor = _reserve_floor(problem)
    energy = battery.current_soc_percent / 100.0 * cap if battery else 0.0
    running: list[ApplianceSpec] = [a for a in problem.appliances if placed_names is None or a.name in placed_names]

    cost = imp_total = exp_total = unserved_total = 0.0
    for t in range(n):
        slot = problem.slots[t]
        load = slot.base_load_kw + sum(a.rated_kw for a in running if a.preferred() <= t < a.preferred() + a.duration_slots)
        solar = min(slot.solar_kw, inverter) if inverter is not None else slot.solar_kw
        charge = discharge = 0.0
        if battery is not None and cap > 0:
            if solar > load:
                room = (cap - energy) / (eta * dt)
                charge = max(0.0, min(solar - load, battery.max_charge_kw, inverter if inverter is not None else math.inf, room))
            else:
                headroom = inverter - solar if inverter is not None else math.inf
                usable = max(0.0, energy - floor) * eta / dt
                discharge = max(0.0, min(load - solar, battery.max_discharge_kw, max(0.0, headroom), usable))
            energy += charge * dt * eta - discharge * dt / eta
        net = load - solar - discharge + charge
        import_cap = grid.sanctioned_load_kw if slot.grid_available else 0.0
        if net > 0:
            imported, exported, unserved = min(net, import_cap), 0.0, max(0.0, net - import_cap)
        else:
            export_cap = 0.0 if (grid.zero_export or not slot.grid_available) else (grid.max_export_kw if grid.max_export_kw is not None else math.inf)
            imported, exported, unserved = 0.0, min(-net, export_cap), 0.0
        cost += dt * (imported * slot.import_price_inr_per_kwh - exported * slot.export_credit_inr_per_kwh)
        imp_total += imported * dt
        exp_total += exported * dt
        unserved_total += unserved * dt
    return {
        "cost_inr": cost,
        "grid_import_kwh": imp_total,
        "grid_export_kwh": exp_total,
        "unserved_kwh": unserved_total,
        "final_soc_percent": energy / cap * 100.0 if cap > 0 else None,
    }
