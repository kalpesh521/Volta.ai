"""
`optimize(problem)`: the optimizer engine's single entry point.

Pipeline
--------
 1. build the shared context (slot physics, battery limits, goal weights)
 2. place shiftable appliances (appliances.py: exact for one, coordinate descent for several)
 3. solve the battery exactly for the resulting load profile (battery.py: DP over the SOC grid)
 4. execute the plan against the TRUE battery state and compare it with two safety nets:
      - the default self-consumption policy on the same loads (guards against grid-engine rounding), and
      - the full baseline (default policy, appliances at their preferred start)
    The cheapest by objective wins, ties to the DP. So the answer is never worse than doing nothing.
 5. run the INDEPENDENT verifier (verify.py) on the assembled result. A plan with any violation is never
    returned as a plan: `status="infeasible"` with the violations (or `PlanVerificationError` if strict).

Known limits (also in the facts' `meta`): exactly optimal for the battery and for at most one shiftable
appliance; with several it is a coordinate-wise optimum (`feasible_heuristic`); the horizon is optimised
as given (no value for energy left in the battery unless `terminal_value_inr_per_kwh` says so), and the
forecast is taken as certain: re-plan when forecasts change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .appliances import (
    UNSERVED_SENTINEL_INR,
    Placement,
    compose,
    evaluate_starts,
    fixed_loads,
    place_appliances,
)
from .battery import (
    Context,
    SlotExec,
    baseline_intents,
    build_context,
    execute,
    score,
)
from .engine import BatteryEngine, make_engine
from .models import (
    ApplianceSchedule,
    Goal,
    OptimizationProblem,
    OptimizationResult,
    PlanTotals,
    PlanVerificationError,
    SlotPlan,
    Status,
    Violation,
)
from .verify import verify_plan

logger = logging.getLogger(__name__)

_IDLE_KW = 0.005


@dataclass(frozen=True)
class _Candidate:
    name: str
    loads: list[float]
    starts: dict[str, int]  # shiftable appliance starts used by this candidate
    execs: list[SlotExec]
    objective: float


def optimize(problem: OptimizationProblem, *, strict: bool = False) -> OptimizationResult:
    """Solve `problem`. Pure and deterministic. See the module docstring for what is guaranteed."""
    ctx = build_context(problem)
    dp, engine_name = make_engine(problem, ctx)
    placement = place_appliances(problem, ctx, dp)

    blocked = [
        Violation(
            code="appliance_unplaceable",
            message=f"{spec.name} has no feasible start: it would exceed the sanctioned load or the window is too tight",
        )
        for spec in problem.appliances
        if spec.name in placement.unplaced and (spec.must_run or spec.critical)
    ]
    if blocked:
        return _infeasible(problem, tuple(blocked), ctx.warnings)

    fixed = fixed_loads(problem)
    shiftable = {a.name: a for a in problem.appliances if not a.critical}
    placed = [(shiftable[name], start) for name, start in sorted(placement.starts.items())]
    preferred = [(spec, spec.preferred()) for spec, _ in placed]

    candidates = [
        _dp_candidate(ctx, dp, fixed, placed, placement, problem),
        _policy_candidate("baseline_policy_on_plan_loads", ctx, fixed, placed, placement.starts, respect_hold=True),
        _policy_candidate("baseline", ctx, fixed, preferred, {s.name: s.preferred() for s, _ in placed}, respect_hold=False),
    ]
    # The full baseline is a legitimate answer only when it obeys the goal's own hard rules: under
    # maximize_backup it cycles the battery on-grid, which that goal forbids, so it can be compared with
    # (it is the baseline in the savings) but never returned as the plan.
    eligible = candidates if problem.goal is not Goal.MAXIMIZE_BACKUP else candidates[:2]
    chosen = eligible[0]
    for candidate in eligible[1:]:
        if candidate.objective < chosen.objective - 1e-9:
            chosen = candidate

    baseline_candidate = candidates[2]
    result = _assemble(problem, ctx, dp, chosen, baseline_candidate, placement, engine_name)
    violations = verify_plan(problem, result)
    if violations:
        logger.error("optimizer produced an invalid plan: %s", violations[:3])
        if strict:
            raise PlanVerificationError(tuple(violations))
        return _infeasible(problem, tuple(violations), ctx.warnings)
    return result


# ---------------------------------------------------------------------------------------- candidates


def _dp_candidate(
    ctx: Context,
    dp: BatteryEngine,
    fixed: list[float],
    placed: list[tuple],
    placement: Placement,
    problem: OptimizationProblem,
) -> _Candidate:
    loads = compose(fixed, placed)
    execs = dp.run(loads)
    return _Candidate("dp", loads, dict(placement.starts), execs, score(ctx, execs))


def _policy_candidate(
    name: str, ctx: Context, fixed: list[float], placed: list[tuple], starts: dict[str, int], *, respect_hold: bool
) -> _Candidate:
    loads = compose(fixed, placed)
    execs = execute(ctx, loads, baseline_intents(ctx, loads), allow_grid_charging=False, respect_hold=respect_hold)
    return _Candidate(name, loads, dict(starts), execs, score(ctx, execs))


# ------------------------------------------------------------------------------------------ assembly


def _assemble(
    problem: OptimizationProblem,
    ctx: Context,
    dp: BatteryEngine,
    chosen: _Candidate,
    baseline: _Candidate,
    placement: Placement,
    engine_name: str,
) -> OptimizationResult:
    appliance_kw = _appliance_profile(problem, chosen.starts)
    slots = tuple(_slot_plan(problem, ctx, t, chosen.execs[t], appliance_kw[t]) for t in range(problem.n_slots))
    schedule = _schedule(problem, ctx, dp, chosen)
    totals = _totals(problem, ctx, chosen.execs)
    base_totals = _totals(problem, ctx, baseline.execs)

    savings = base_totals.cost_inr - totals.cost_inr
    percent = savings / base_totals.cost_inr * 100.0 if base_totals.cost_inr > 1e-9 else 0.0
    shiftable_count = sum(1 for a in problem.appliances if not a.critical and a.name in placement.starts)
    # The exact engine is optimal for the battery; the grid engine is only near-optimal (see grid_dp.py).
    approximate_battery = engine_name == "grid" and ctx.params is not None
    heuristic = shiftable_count > 1 or chosen.name != "dp" or approximate_battery

    warnings = list(ctx.warnings)
    warnings.extend(_warnings(problem, totals, base_totals, schedule, placement, slots))
    meta = {
        "selected_plan": chosen.name,
        "battery_engine": engine_name,
        "soc_step_percent": problem.soc_step_percent if engine_name == "grid" else None,
        "descent_rounds": placement.rounds,
        "descent_converged": placement.converged,
        "objective_inr_equivalent": round(chosen.objective, 4),
        "baseline_objective_inr_equivalent": round(baseline.objective, 4),
        "battery_reserve_kwh": round(ctx.params.floor_kwh, 4) if ctx.params else None,
        "battery_capacity_kwh": ctx.params.capacity_kwh if ctx.params else None,
    }
    return OptimizationResult(
        status=Status.FEASIBLE_HEURISTIC if heuristic else Status.OPTIMAL,
        goal=problem.goal,
        slot_minutes=problem.slot_minutes,
        slots=slots,
        appliances=schedule,
        totals=totals,
        baseline=base_totals,
        savings_inr=savings,
        savings_kwh=base_totals.grid_import_kwh - totals.grid_import_kwh,
        savings_percent=percent,
        warnings=tuple(warnings),
        meta=meta,
    )


def _appliance_profile(problem: OptimizationProblem, starts: dict[str, int]) -> list[float]:
    kw = [0.0] * problem.n_slots
    for spec in problem.appliances:
        start = spec.preferred() if spec.critical else starts.get(spec.name)
        if start is None:
            continue
        for t in range(start, start + spec.duration_slots):
            kw[t] += spec.rated_kw
    return kw


def _slot_plan(problem: OptimizationProblem, ctx: Context, t: int, ex: SlotExec, appliance_kw: float) -> SlotPlan:
    slot, env = problem.slots[t], ctx.envs[t]
    cap = ctx.params.capacity_kwh if ctx.params else 0.0
    cost = ctx.dt * (ex.flows.grid_import * env.price - ex.flows.grid_export * env.credit)
    action = "charge" if ex.charge_kw > _IDLE_KW else "discharge" if ex.discharge_kw > _IDLE_KW else "idle"
    return SlotPlan(
        index=t,
        start=slot.start,
        grid_available=slot.grid_available,
        solar_kw=slot.solar_kw,
        base_load_kw=slot.base_load_kw,
        appliance_kw=appliance_kw,
        load_kw=ex.load_kw,
        solar_used_kw=slot.solar_kw - ex.flows.curtailed,
        curtailed_kw=ex.flows.curtailed,
        battery_charge_kw=ex.charge_kw,
        battery_discharge_kw=ex.discharge_kw,
        grid_import_kw=ex.flows.grid_import,
        grid_export_kw=ex.flows.grid_export,
        unserved_kw=ex.flows.unserved,
        soc_kwh=ex.soc_kwh,
        soc_percent=ex.soc_kwh / cap * 100.0 if cap > 0 else 0.0,
        import_price_inr_per_kwh=env.price,
        export_credit_inr_per_kwh=env.credit,
        cost_inr=cost,
        action=action,
    )


def _schedule(
    problem: OptimizationProblem, ctx: Context, dp: BatteryEngine, chosen: _Candidate
) -> tuple[ApplianceSchedule, ...]:
    out: list[ApplianceSchedule] = []
    for spec in problem.appliances:
        pref = spec.preferred()
        start = pref if spec.critical else chosen.starts.get(spec.name)
        placed = start is not None
        saves: float | None = None
        preferred_feasible = True
        if placed and start != pref:
            others = [(o, chosen.starts[o.name]) for o in problem.appliances if not o.critical and o.name in chosen.starts and o.name != spec.name]
            values = evaluate_starts(problem, ctx, dp, compose(fixed_loads(problem), others), spec)
            if pref in values and start in values and values[pref] - values[start] < UNSERVED_SENTINEL_INR:
                saves = max(0.0, values[pref] - values[start])
            else:
                preferred_feasible = pref in values and values[pref] - min(values.values()) < UNSERVED_SENTINEL_INR
        elif placed:
            saves = 0.0
        out.append(
            ApplianceSchedule(
                name=spec.name,
                rated_kw=spec.rated_kw,
                duration_slots=spec.duration_slots,
                critical=spec.critical,
                must_run=spec.must_run,
                placed=placed,
                start_slot=start,
                start=problem.slots[start].start if placed else None,
                end=_end_time(problem, start, spec.duration_slots) if placed else None,
                preferred_start_slot=pref,
                preferred_start=problem.slots[pref].start,
                shifted=placed and start != pref,
                saves_inr=saves,
                preferred_feasible=preferred_feasible,
            )
        )
    return tuple(out)


def _end_time(problem: OptimizationProblem, start: int, duration: int) -> datetime:
    return problem.slots[start].start + timedelta(minutes=problem.slot_minutes * duration)


def _totals(problem: OptimizationProblem, ctx: Context, execs: list[SlotExec]) -> PlanTotals:
    dt = ctx.dt
    cost = imp = exp = solar = used = curtailed = load = unserved = charged = discharged = 0.0
    for slot, env, ex in zip(problem.slots, ctx.envs, execs):
        f = ex.flows
        cost += dt * (f.grid_import * env.price - f.grid_export * env.credit)
        imp += f.grid_import * dt
        exp += f.grid_export * dt
        solar += slot.solar_kw * dt
        used += (slot.solar_kw - f.curtailed) * dt
        curtailed += f.curtailed * dt
        load += ex.load_kw * dt
        unserved += f.unserved * dt
        charged += ex.charge_kw * dt
        discharged += ex.discharge_kw * dt
    onsite = max(0.0, used - exp)
    self_consumption = min(100.0, onsite / solar * 100.0) if solar > 0 else 0.0
    self_sufficiency = min(100.0, max(0.0, (load - imp) / load * 100.0)) if load > 0 else 0.0
    cap = ctx.params.capacity_kwh if ctx.params else 0.0
    final_soc = execs[-1].soc_kwh / cap * 100.0 if cap > 0 and execs else None
    return PlanTotals(
        cost_inr=cost,
        grid_import_kwh=imp,
        grid_export_kwh=exp,
        solar_kwh=solar,
        solar_used_kwh=used,
        curtailed_kwh=curtailed,
        load_kwh=load,
        unserved_kwh=unserved,
        battery_charged_kwh=charged,
        battery_discharged_kwh=discharged,
        self_consumption_percent=self_consumption,
        self_sufficiency_percent=self_sufficiency,
        final_soc_percent=final_soc,
    )


def _warnings(
    problem: OptimizationProblem,
    totals: PlanTotals,
    baseline: PlanTotals,
    schedule: tuple[ApplianceSchedule, ...],
    placement: Placement,
    slots: tuple[SlotPlan, ...],
) -> list[str]:
    out: list[str] = []
    if totals.unserved_kwh > 1e-6:
        hours = ", ".join(s.start.strftime("%H:%M") for s in slots if s.unserved_kw > 1e-6)
        out.append(f"{totals.unserved_kwh:.1f} kWh of load cannot be served (slots starting {hours}).")
    for item in schedule:
        if not item.placed and not item.must_run:
            out.append(f"{item.name} could not be scheduled and was left out.")
        if item.placed and not item.preferred_feasible:
            out.append(f"{item.name} cannot run at its preferred time without exceeding the sanctioned load.")
    if problem.grid.zero_export and totals.curtailed_kwh > 0.05:
        out.append(f"{totals.curtailed_kwh:.1f} kWh of solar is curtailed because export is switched off.")
    if not placement.converged and len(placement.starts) > 1:
        out.append("Appliance placement did not fully converge; the schedule is a good, not a proven best, one.")
    return out


def _infeasible(
    problem: OptimizationProblem, violations: tuple[Violation, ...], warnings: tuple[str, ...]
) -> OptimizationResult:
    return OptimizationResult(
        status=Status.INFEASIBLE,
        goal=problem.goal,
        slot_minutes=problem.slot_minutes,
        slots=(),
        appliances=(),
        totals=None,
        baseline=None,
        savings_inr=0.0,
        savings_kwh=0.0,
        savings_percent=0.0,
        violations=violations,
        warnings=warnings,
        meta={},
    )


__all__ = ["optimize"]
