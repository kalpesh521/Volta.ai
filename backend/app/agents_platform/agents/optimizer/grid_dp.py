"""
SOC-grid dynamic program (the literal "DP over a discretised state of charge").

Why it exists next to the exact engine (`convex_dp.py`): it needs no convexity, so it also solves the
cases the exact engine cannot, namely a battery that STARTS BELOW its reserve (states below the floor
exist and may only move up). It is also an independent implementation the tests use to cross-check the
exact engine (same problem, fine grid, near-equal optimum).

Accuracy: first order in the grid step. The value function is convex but kinks fall between nodes, and
linear interpolation overestimates a convex function at a kink, which biases decisions toward moves that
land on nodes. A 1 % grid is usually within a few percent of the exact optimum on bill-sized problems and
can be several percent off on steep (self-consumption) objectives. The solver's safety net (never worse
than the default policy) covers the rest. Prefer the exact engine whenever it applies.
"""
from __future__ import annotations

import math
from typing import NamedTuple

from .battery import (
    INF,
    Context,
    SlotEnv,
    SlotExec,
    charge_available,
    discharge_available,
    resolve_flows,
    slot_objective,
)

_EPS = 1e-9
_TIE = 1e-9  # objectives closer than this are a tie; ties resolve to the smaller battery move


class Action(NamedTuple):
    """One battery decision for a slot, with its cost and where it lands on the SOC grid."""

    charge_kw: float
    discharge_kw: float
    delta_kwh: float  # change of stored energy
    cost: float  # slot objective (grid money + goal penalties + unserved penalty)
    delta_idx: float  # delta_kwh in grid steps
    n0: int  # floor(delta_idx)
    frac: float  # delta_idx - n0, snapped to 0 when negligible


class _Table(NamedTuple):
    """Actions for one (slot, load), plus the two state-dependent moves that land exactly on a boundary."""

    actions: list[Action]
    drain: list[float]  # per node: cost of discharging exactly down to the reserve floor (INF if not possible)
    fill: list[float]  # per node: cost of charging exactly up to full (INF if not possible)


class GridBatteryDP:
    """
    Backward value iteration on a uniform energy grid with LINEAR INTERPOLATION of the value function.

    Nodes run from the reserve floor to full (both are nodes), plus extra nodes below the floor only when
    the battery starts below its reserve. Actions are the grid multiples of charge/discharge power PLUS
    the two natural ones (charge as much as the surplus/limits allow, discharge exactly what the load
    needs) PLUS "drain to the reserve" and "fill to full", which depend on the starting energy and let the
    last sliver of usable energy be used and the last sliver of headroom be filled. A destination that falls between nodes is valued by interpolating its neighbours, so the plan
    is not forced to move in whole SOC steps (a 1 % step at 30-minute slots would otherwise be a coarse
    power step and cost real money). The value function is convex piecewise linear in energy for these
    costs, so interpolation error is second order in the step.

    The plan is then PLAYED FORWARD from the exact starting energy (`run`): each slot picks the action
    minimising slot cost + interpolated cost-to-go at the TRUE energy, so reported flows and SOC are exact
    and every bound holds by construction (the verifier re-checks them independently).
    """

    def __init__(self, ctx: Context, soc_step_percent: float) -> None:
        self.ctx = ctx
        self.n = len(ctx.envs)
        self._tables: dict[tuple[int, float], _Table] = {}
        params = ctx.params
        if params is None:
            self.K, self.kfloor, self.step = 1, 0, 1.0
            self.energies = [0.0]
            self.unit_c = self.unit_d = 1.0
        else:
            target = params.capacity_kwh * soc_step_percent / 100.0
            span = params.capacity_kwh - params.floor_kwh
            kint = max(1, math.ceil(span / target - _EPS)) if span > _EPS else 0
            self.step = span / kint if kint else target
            below = max(0, math.ceil((params.floor_kwh - params.e0_kwh) / self.step - _EPS))
            self.kfloor = below
            self.K = below + kint + 1
            lowest = params.floor_kwh - below * self.step
            self.energies = [lowest + k * self.step for k in range(self.K)]
            self.unit_c = self.step / (params.eta * ctx.dt)  # kW of charge per grid step
            self.unit_d = self.step * params.eta / ctx.dt  # kW of discharge per grid step
        w = ctx.weights
        if params is not None and w.shortfall_penalty > 0:
            self.state_cost = [w.shortfall_penalty * ctx.dt * max(0.0, params.floor_kwh - e) for e in self.energies]
        else:
            self.state_cost = [0.0] * self.K
        self.terminal = [-w.terminal_value * e for e in self.energies]

    # -- per-slot action table -----------------------------------------------------------------

    def table(self, t: int, load_kw: float) -> _Table:
        """Feasible actions for slot t with this total load, cheapest-to-hold first (idle, then by size)."""
        key = (t, round(load_kw, 9))
        cached = self._tables.get(key)
        if cached is not None:
            return cached
        ctx, env = self.ctx, self.ctx.envs[t]
        params = ctx.params
        actions = [self._action(env, load_kw, 0.0, 0.0)]
        drain = [INF] * self.K
        fill = [INF] * self.K
        if params is not None and self.K > 1:
            c_avail = charge_available(env, load_kw, ctx.allow_grid_charging)
            d_avail = discharge_available(env, load_kw)
            for power, unit, charging in ((c_avail, self.unit_c, True), (d_avail, self.unit_d, False)):
                if power <= 1e-9:
                    continue
                powers = [k * unit for k in range(1, min(self.K - 1, int(power / unit + _EPS)) + 1)]
                if not powers or power - powers[-1] > 1e-9:
                    powers.append(power)  # the natural action: use everything the rules allow
                for p in powers:
                    actions.append(self._action(env, load_kw, p if charging else 0.0, 0.0 if charging else p))
            for k in range(self.K):
                to_floor = self.drain_power(self.energies[k])
                if 1e-9 < to_floor <= d_avail + 1e-9:
                    drain[k] = self._action(env, load_kw, 0.0, to_floor).cost
                to_full = self.fill_power(self.energies[k])
                if 1e-9 < to_full <= c_avail + 1e-9:
                    fill[k] = self._action(env, load_kw, to_full, 0.0).cost
        table = _Table(actions, drain, fill)
        self._tables[key] = table
        return table

    def drain_power(self, energy: float) -> float:
        """Discharge power that takes `energy` exactly down to the reserve floor (0 if at or below it)."""
        params = self.ctx.params
        assert params is not None
        return max(0.0, energy - params.floor_kwh) * params.eta / self.ctx.dt

    def fill_power(self, energy: float) -> float:
        """Charge power that takes `energy` exactly up to full."""
        params = self.ctx.params
        assert params is not None
        return max(0.0, params.capacity_kwh - energy) / (params.eta * self.ctx.dt)

    def _action(self, env: SlotEnv, load_kw: float, charge: float, discharge: float) -> Action:
        ctx = self.ctx
        params = ctx.params
        cost = slot_objective(env, resolve_flows(env, load_kw, charge, discharge), ctx.dt, ctx.weights)
        if params is None:
            return Action(0.0, 0.0, 0.0, cost, 0.0, 0, 0.0)
        delta = charge * ctx.dt * params.eta - discharge * ctx.dt / params.eta
        idx = delta / self.step
        n0 = math.floor(idx + 1e-9)
        frac = idx - n0
        if frac < 1e-9:
            frac = 0.0
        return Action(charge, discharge, delta, cost, idx, n0, frac)

    def _src_range(self, a: Action) -> tuple[int, int]:
        """Source nodes [lo, hi) from which the action lands inside [reserve, full]."""
        x_min = self.kfloor if a.delta_idx < -1e-12 else 0
        x_max = self.K - 1
        lo = max(0, math.ceil(x_min - a.delta_idx - 1e-9))
        hi = min(self.K - 1, math.floor(x_max - a.delta_idx + 1e-9)) + 1
        return lo, hi

    # -- recursion -----------------------------------------------------------------------------

    def _relax(self, nxt: list[float], table: _Table) -> list[float]:
        weighted = [v + c for v, c in zip(nxt, self.state_cost)]
        cur = [INF] * self.K
        for a in table.actions:
            lo, hi = self._src_range(a)
            n0, f, cost = a.n0, a.frac, a.cost
            if f == 0.0:
                for k in range(lo, hi):
                    v = cost + weighted[k + n0]
                    if v < cur[k]:
                        cur[k] = v
            else:
                g = 1.0 - f
                for k in range(lo, hi):
                    i = k + n0
                    v = cost + g * weighted[i] + f * weighted[i + 1]
                    if v < cur[k]:
                        cur[k] = v
        if self.K > 1:
            to_floor, to_full = weighted[self.kfloor], weighted[-1]
            for k in range(self.kfloor + 1, self.K):
                v = table.drain[k] + to_floor
                if v < cur[k]:
                    cur[k] = v
            for k in range(self.K - 1):
                v = table.fill[k] + to_full
                if v < cur[k]:
                    cur[k] = v
        return cur

    def backward(self, loads: list[float]) -> list[list[float]]:
        """V[t][k]: cheapest cost from grid node k at the start of slot t to the end, incl. terminal value."""
        out = [self.terminal]
        for t in range(self.n - 1, -1, -1):
            out.append(self._relax(out[-1], self.table(t, loads[t])))
        out.reverse()
        return out

    def _interp(self, values: list[float], energy: float) -> float:
        x = (energy - self.energies[0]) / self.step
        if x <= 0.0:
            return values[0]
        i = int(x)
        if i >= self.K - 1:
            return values[-1]
        f = x - i
        return values[i] if f < 1e-12 else (1.0 - f) * values[i] + f * values[i + 1]

    def value(self, loads: list[float], backward: list[list[float]] | None = None) -> float:
        back = backward if backward is not None else self.backward(loads)
        params = self.ctx.params
        return self._interp(back[0], params.e0_kwh if params else 0.0)

    def candidate_value(
        self, backward: list[list[float]], base_loads: list[float], start: int, new_loads: list[float]
    ) -> float:
        """
        Optimal objective if slots start..start+len(new_loads)-1 carry `new_loads` and every other slot keeps
        `base_loads` (for which `backward` was computed). Slots after the change reuse `backward`; only the
        changed slots and those before them are re-run, which keeps enumerating start times affordable.
        """
        stop = start + len(new_loads)
        values = backward[stop]
        for t in range(stop - 1, -1, -1):
            load = new_loads[t - start] if t >= start else base_loads[t]
            values = self._relax(values, self.table(t, load))
        params = self.ctx.params
        return self._interp(values, params.e0_kwh if params else 0.0)

    def _boundary_moves(self, t: int, load_kw: float, energy: float) -> list[tuple[float, float, float, float]]:
        """The two state-dependent moves at the TRUE energy: drain exactly to the reserve, fill exactly to full."""
        ctx, env = self.ctx, self.ctx.envs[t]
        params = ctx.params
        assert params is not None
        out: list[tuple[float, float, float, float]] = []
        to_floor = self.drain_power(energy)
        if 1e-9 < to_floor <= discharge_available(env, load_kw) + 1e-9:
            cost = self._action(env, load_kw, 0.0, to_floor).cost
            out.append((cost, 0.0, to_floor, params.floor_kwh))
        to_full = self.fill_power(energy)
        if 1e-9 < to_full <= charge_available(env, load_kw, ctx.allow_grid_charging) + 1e-9:
            out.append((self._action(env, load_kw, to_full, 0.0).cost, to_full, 0.0, params.capacity_kwh))
        return out

    def run(self, loads: list[float]) -> list[SlotExec]:
        """Play the optimal policy forward from the true starting energy (see class docstring)."""
        ctx = self.ctx
        params = ctx.params
        back = self.backward(loads)
        energy = params.e0_kwh if params else 0.0
        out: list[SlotExec] = []
        for t in range(self.n):
            weighted = [v + c for v, c in zip(back[t + 1], self.state_cost)]
            best: tuple[float, float, float, float] | None = None  # (objective, charge, discharge, next energy)
            table = self.table(t, loads[t])
            moves = [(a.cost, a.charge_kw, a.discharge_kw, energy + a.delta_kwh) for a in table.actions]
            if params is not None and self.K > 1:
                moves.extend(self._boundary_moves(t, loads[t], energy))
            for cost, charge, discharge, nxt in moves:
                if params is not None:
                    if discharge > 0 and nxt < params.floor_kwh - 1e-9:
                        continue
                    if charge > 0:
                        if nxt > params.capacity_kwh + 1e-9:
                            continue
                v = cost + self._interp(weighted, nxt)
                if best is None or v < best[0] - _TIE:
                    best = (v, charge, discharge, nxt)
            assert best is not None  # idle is always legal
            _, charge, discharge, energy = best
            if params is not None:
                energy = min(energy, params.capacity_kwh)
            env = ctx.envs[t]
            out.append(
                SlotExec(
                    loads[t],
                    charge,
                    discharge,
                    resolve_flows(env, loads[t], charge, discharge),
                    energy,
                )
            )
        return out


