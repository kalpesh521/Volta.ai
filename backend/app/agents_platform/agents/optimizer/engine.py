"""
Choosing the battery engine.

Two engines solve the same problem behind one interface (`backward`, `value`, `candidate_value`, `run`):

* `ConvexBatteryDP` (default): exact, no grid, milliseconds. Needs the battery to start at or above its
  reserve.
* `GridBatteryDP`: the SOC-grid DP, used when the battery starts BELOW its reserve, when there is no
  battery (a one-state grid, so appliance placement still works), or when the caller asks for it
  (`battery_engine="grid"`, e.g. to cross-check the exact engine).
"""
from __future__ import annotations

from typing import Any, Protocol

from .battery import Context, SlotExec
from .convex_dp import ConvexBatteryDP
from .grid_dp import GridBatteryDP
from .models import OptimizationProblem


class BatteryEngine(Protocol):
    def backward(self, loads: list[float]) -> list[Any]: ...

    def value(self, loads: list[float], backward: list[Any] | None = None) -> float: ...

    def candidate_value(self, backward: list[Any], base_loads: list[float], start: int, new_loads: list[float]) -> float: ...

    def run(self, loads: list[float]) -> list[SlotExec]: ...


def make_engine(problem: OptimizationProblem, ctx: Context) -> tuple[BatteryEngine, str]:
    params = ctx.params
    if problem.battery_engine == "exact" and params is not None and params.e0_kwh >= params.floor_kwh - 1e-9:
        return ConvexBatteryDP(ctx), "exact"
    return GridBatteryDP(ctx, problem.soc_step_percent), "grid"
