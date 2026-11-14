"""
Placing shiftable appliances on the timeline.

Algorithm: coordinate descent over appliances, exact per appliance
------------------------------------------------------------------
For ONE appliance the problem is solved exactly: enumerate every start whose run fits its window, price
each by re-solving the battery DP for the resulting load profile, keep the cheapest. That is optimal
(for the SOC grid) because the appliance has no other degree of freedom.

For SEVERAL appliances the choices interact (they share the battery, the sanctioned load and the price
curve), and the joint search is exponential. So the engine does what is standard and honest:

  round 1  : insert appliances one at a time, most-constrained first (fewest feasible starts, then largest
             energy, then name) so a tight window is never blocked by a flexible neighbour. Each sees only
             the appliances already placed.
  round 2+ : re-optimise each appliance with all the others fixed, in the same order, until a full round
             changes nothing or `max_descent_rounds` is reached.

Every step picks the best start for one appliance given the rest, so the total objective never gets worse
from a re-optimisation, but the result is a COORDINATE-WISE optimum, not a proven global one. The solver
therefore reports `feasible_heuristic` (not `optimal`) whenever two or more shiftable appliances exist.
Order is fixed and tie-breaks are fixed, so the same input always yields the same plan.

Inconvenience guard: the preferred start wins unless another start is cheaper by more than
`min_shift_saving_inr`, so the household is never asked to run the washing machine at 3 a.m. to save 2 paise.

Sanctioned load: a start is infeasible if, in any slot of the run, the load that solar, the battery's
discharge limit and the sanctioned import cannot together cover. (Energy shortage, e.g. an empty battery
in an outage, is left to the DP, which prices unserved load prohibitively and reports it.)
"""
from __future__ import annotations

from dataclasses import dataclass

from .battery import Context
from .engine import BatteryEngine
from .models import UNSERVED_PENALTY_INR_PER_KWH, ApplianceSpec, OptimizationProblem

_TIE = 1e-9
# A difference in objective this large can only come from unserved load, never from money.
UNSERVED_SENTINEL_INR = UNSERVED_PENALTY_INR_PER_KWH * 0.01


@dataclass(frozen=True)
class Placement:
    starts: dict[str, int]  # shiftable appliances that were placed
    unplaced: tuple[str, ...]
    rounds: int
    converged: bool


def fixed_loads(problem: OptimizationProblem) -> list[float]:
    """Base load plus critical appliances, which never move (they run at their preferred start)."""
    loads = [slot.base_load_kw for slot in problem.slots]
    for spec in problem.appliances:
        if spec.critical:
            _add(loads, spec, spec.preferred())
    return loads


def compose(fixed: list[float], placed: list[tuple[ApplianceSpec, int]]) -> list[float]:
    loads = list(fixed)
    for spec, start in placed:
        _add(loads, spec, start)
    return loads


def _add(loads: list[float], spec: ApplianceSpec, start: int) -> None:
    for t in range(start, start + spec.duration_slots):
        loads[t] += spec.rated_kw


def static_feasible(ctx: Context, base_loads: list[float], spec: ApplianceSpec, start: int) -> bool:
    """Could solar + discharge limit + sanctioned import cover the load in every slot of this run?"""
    for t in range(start, start + spec.duration_slots):
        env = ctx.envs[t]
        need = base_loads[t] + spec.rated_kw - env.solar_eff_kw
        if need > env.import_cap_kw + env.discharge_cap_kw + 1e-9:
            return False
    return True


def evaluate_starts(
    problem: OptimizationProblem, ctx: Context, dp: BatteryEngine, base_loads: list[float], spec: ApplianceSpec
) -> dict[int, float]:
    """Optimal objective for every feasible start of `spec`, with `base_loads` holding everything else."""
    backward = dp.backward(base_loads)
    values: dict[int, float] = {}
    for start in spec.feasible_starts(problem.n_slots):
        if not static_feasible(ctx, base_loads, spec, start):
            continue
        run = [base_loads[t] + spec.rated_kw for t in range(start, start + spec.duration_slots)]
        values[start] = dp.candidate_value(backward, base_loads, start, run)
    return values


def choose_start(values: dict[int, float], spec: ApplianceSpec, min_shift_saving_inr: float) -> int | None:
    """Cheapest start; ties go to the one nearest the preferred time; small savings do not justify a shift."""
    if not values:
        return None
    preferred = spec.preferred()
    best = min(values, key=lambda s: (round(values[s] / _TIE) * _TIE, abs(s - preferred), s))
    if preferred in values and values[preferred] - values[best] <= min_shift_saving_inr:
        return preferred
    return best


def placement_order(problem: OptimizationProblem) -> list[ApplianceSpec]:
    shiftable = [a for a in problem.appliances if not a.critical]
    n = problem.n_slots
    return sorted(shiftable, key=lambda a: (len(a.feasible_starts(n)), -a.rated_kw * a.duration_slots, a.name))


def place_appliances(problem: OptimizationProblem, ctx: Context, dp: BatteryEngine) -> Placement:
    order = placement_order(problem)
    fixed = fixed_loads(problem)
    starts: dict[str, int] = {}
    unplaced: dict[str, None] = {}
    if not order:
        return Placement({}, (), 0, True)

    max_rounds = problem.max_descent_rounds if len(order) > 1 else 1
    rounds = 0
    converged = False
    for rnd in range(max_rounds):
        rounds += 1
        changed = False
        for spec in order:
            others = [(o, starts[o.name]) for o in order if o.name in starts and o.name != spec.name]
            base = compose(fixed, others)
            choice = choose_start(evaluate_starts(problem, ctx, dp, base, spec), spec, problem.min_shift_saving_inr)
            if choice is None:
                unplaced[spec.name] = None
                if starts.pop(spec.name, None) is not None:
                    changed = True
                continue
            unplaced.pop(spec.name, None)
            if starts.get(spec.name) != choice:
                changed = True
            starts[spec.name] = choice
        if rnd > 0 and not changed:
            converged = True
            break
    else:
        converged = len(order) <= 1
    return Placement(starts, tuple(unplaced), rounds, converged)
