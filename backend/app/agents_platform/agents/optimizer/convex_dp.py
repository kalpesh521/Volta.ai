"""
Exact battery dynamic program: the value function is kept as a convex piecewise-linear function.

The idea
--------
For a fixed load profile the cost-to-go V_t(e) (cheapest way to finish the horizon from stored energy e
at the start of slot t) satisfies the Bellman recursion

    V_t(e) = min over battery moves D of  h_t(D) + V_{t+1}(e + D)          e, e + D in [reserve, full]

where D is the change of stored energy and h_t(D) is the slot's grid cost for that move. Every ingredient
is convex and piecewise linear: the terminal value is linear, h_t is convex because net grid power is
convex in D (discharging is less "efficient per kWh stored" than charging is "costly per kWh stored":
slopes eta/dt vs 1/(eta*dt)) and the tariff slopes are ordered (curtail 0 <= export <= import <= unserved,
which `battery.slot_objective` guarantees). The minimum of two convex functions of (e + D) and D is their
INFIMAL CONVOLUTION, and for piecewise-linear functions that is just "concatenate the segments sorted by
slope". So the DP is exact (no SOC grid, no interpolation) and costs O(slots * breakpoints), a few
milliseconds, which is what makes enumerating every start time of every appliance cheap.

Playing the policy forward
--------------------------
`run` starts from the true energy and, per slot, picks the move minimising h_t(D) + V_{t+1}(e + D). A
convex minimum sits at a breakpoint of either function or at a boundary, so only those are tried. Flows
and SOC are computed with the shared physics, then re-checked by the independent verifier.

Limits: needs e0 >= reserve (a battery below its reserve is routed to the grid engine, whose states may
sit below the floor) and a convex per-slot cost (guaranteed by the objective clamps; a numerical hiccup
is repaired by projecting slopes to be non-decreasing).
"""
from __future__ import annotations

import math
from bisect import bisect_right

from .battery import (
    Context,
    SlotExec,
    charge_available,
    discharge_available,
    resolve_flows,
    slot_objective,
)

_TIE = 1e-9
_LEN_EPS = 1e-12
Segments = list[tuple[float, float]]  # (length, slope), slopes non-decreasing


class _Pwl:
    """Convex piecewise-linear function on [x0, x0 + sum(lengths)] given as (x0, y0, segments)."""

    __slots__ = ("x0", "y0", "segs", "_xs", "_ys")

    def __init__(self, x0: float, y0: float, segs: Segments) -> None:
        self.x0, self.y0, self.segs = x0, y0, segs
        self._xs: list[float] | None = None
        self._ys: list[float] | None = None

    @property
    def x1(self) -> float:
        return self.x0 + sum(length for length, _ in self.segs)

    def arrays(self) -> tuple[list[float], list[float]]:
        if self._xs is None or self._ys is None:
            xs, ys = [self.x0], [self.y0]
            for length, slope in self.segs:
                xs.append(xs[-1] + length)
                ys.append(ys[-1] + slope * length)
            self._xs, self._ys = xs, ys
        return self._xs, self._ys

    def value(self, x: float) -> float:
        xs, ys = self.arrays()
        if x <= xs[0] or len(xs) == 1:
            return ys[0]
        i = bisect_right(xs, x) - 1
        if i >= len(self.segs):
            return ys[-1]
        return ys[i] + self.segs[i][1] * (x - xs[i])


def _coalesce(segs: Segments) -> Segments:
    out: Segments = []
    for length, slope in segs:
        if length <= _LEN_EPS:
            continue
        if out and abs(out[-1][1] - slope) <= 1e-12 * (1.0 + abs(slope)):
            out[-1] = (out[-1][0] + length, out[-1][1])
        else:
            out.append((length, slope))
    return out


def _clip(f: _Pwl, lo: float, hi: float) -> _Pwl:
    """Restrict a convex PWL to [lo, hi] (which it must contain up to rounding)."""
    if hi - lo <= _LEN_EPS:
        return _Pwl(lo, f.value(lo), [])
    pos, y = f.x0, f.y0
    new_x0: float | None = None
    new_y0 = 0.0
    out: Segments = []
    for length, slope in f.segs:
        end = pos + length
        if end > lo + _LEN_EPS and pos < hi - _LEN_EPS:
            a, b = max(pos, lo), min(end, hi)
            if new_x0 is None:
                new_x0, new_y0 = a, y + slope * (a - pos)
            out.append((b - a, slope))
        y += slope * length
        pos = end
    if new_x0 is None:  # domain narrower than numerical noise
        return _Pwl(lo, f.value(lo), [])
    return _Pwl(new_x0, new_y0, out)


class _Move:
    """h_t: convex PWL of the stored-energy change D over [-discharge_room, +charge_room]."""

    __slots__ = ("d_lo", "d_hi", "y_lo", "segs", "_pwl")

    def __init__(self, d_lo: float, y_lo: float, segs: Segments) -> None:
        self.d_lo, self.y_lo, self.segs = d_lo, y_lo, segs
        self.d_hi = d_lo + sum(length for length, _ in segs)
        self._pwl = _Pwl(d_lo, y_lo, segs)

    def value(self, d: float) -> float:
        return self._pwl.value(d)

    def breakpoints(self) -> list[float]:
        return self._pwl.arrays()[0]

    def reflected(self) -> _Pwl:
        """H(s) = h(-s): the form the infimal convolution needs."""
        ys_end = self._pwl.arrays()[1][-1]
        segs = [(length, -slope) for length, slope in reversed(self.segs)]
        return _Pwl(-self.d_hi, ys_end, segs)


def _convolve(v: _Pwl, h: _Pwl) -> _Pwl:
    merged = sorted(v.segs + h.segs, key=lambda seg: seg[1])
    return _Pwl(v.x0 + h.x0, v.y0 + h.y0, _coalesce(merged))


class ConvexBatteryDP:
    """Exact engine with the same interface as `GridBatteryDP`: backward / value / candidate_value / run."""

    def __init__(self, ctx: Context) -> None:
        params = ctx.params
        if params is None:
            raise ValueError("ConvexBatteryDP needs a battery")
        if params.e0_kwh < params.floor_kwh - 1e-9:
            raise ValueError("ConvexBatteryDP needs the battery to start at or above its reserve")
        self.ctx = ctx
        self.n = len(ctx.envs)
        self.lo = params.floor_kwh
        self.hi = params.capacity_kwh
        self._moves: dict[tuple[int, float], _Move] = {}
        tv = ctx.weights.terminal_value
        self._terminal = _Pwl(self.lo, -tv * self.lo, [(self.hi - self.lo, -tv)] if self.hi - self.lo > _LEN_EPS else [])

    # -- per-slot move cost --------------------------------------------------------------------

    def _move(self, t: int, load_kw: float) -> _Move:
        key = (t, round(load_kw, 9))
        cached = self._moves.get(key)
        if cached is not None:
            return cached
        ctx, env, p = self.ctx, self.ctx.envs[t], self.ctx.params
        assert p is not None
        dt, eta = ctx.dt, p.eta
        room_up = charge_available(env, load_kw, ctx.allow_grid_charging) * dt * eta
        room_down = discharge_available(env, load_kw) * dt / eta
        base = load_kw - env.solar_eff_kw

        def delta_for_net(target: float) -> float:
            need = target - base  # net = base + charge - discharge
            return need * eta * dt if need > 0 else need * dt / eta

        points = {-room_down, 0.0, room_up}
        targets = [0.0, env.import_cap_kw]
        if math.isfinite(env.export_cap_kw):
            targets.append(-env.export_cap_kw)
        for target in targets:
            d = delta_for_net(target)
            if -room_down < d < room_up:
                points.add(d)
        ordered = sorted(points)
        costs = [self._cost(t, load_kw, d) for d in ordered]
        segs: Segments = []
        floor_slope = -math.inf
        for (d0, d1), (y0, y1) in zip(zip(ordered, ordered[1:]), zip(costs, costs[1:])):
            length = d1 - d0
            if length <= _LEN_EPS:
                continue
            slope = max((y1 - y0) / length, floor_slope)  # project away rounding noise: convex by construction
            floor_slope = slope
            segs.append((length, slope))
        move = _Move(ordered[0], costs[0], _coalesce(segs))
        self._moves[key] = move
        return move

    def _cost(self, t: int, load_kw: float, delta: float) -> float:
        ctx, env, p = self.ctx, self.ctx.envs[t], self.ctx.params
        assert p is not None
        charge = delta / (p.eta * ctx.dt) if delta > 0 else 0.0
        discharge = -delta * p.eta / ctx.dt if delta < 0 else 0.0
        return slot_objective(env, resolve_flows(env, load_kw, charge, discharge), ctx.dt, ctx.weights)

    # -- recursion -----------------------------------------------------------------------------

    def _step(self, nxt: _Pwl, move: _Move) -> _Pwl:
        return _clip(_convolve(nxt, move.reflected()), self.lo, self.hi)

    def backward(self, loads: list[float]) -> list[_Pwl]:
        """V[t]: convex PWL cost-to-go from the start of slot t; V[n] is the terminal value."""
        out = [self._terminal]
        for t in range(self.n - 1, -1, -1):
            out.append(self._step(out[-1], self._move(t, loads[t])))
        out.reverse()
        return out

    def value(self, loads: list[float], backward: list[_Pwl] | None = None) -> float:
        back = backward if backward is not None else self.backward(loads)
        p = self.ctx.params
        assert p is not None
        return back[0].value(p.e0_kwh)

    def candidate_value(self, backward: list[_Pwl], base_loads: list[float], start: int, new_loads: list[float]) -> float:
        """Optimal objective if slots start.. carry `new_loads`; slots after the change reuse `backward`."""
        stop = start + len(new_loads)
        f = backward[stop]
        for t in range(stop - 1, -1, -1):
            load = new_loads[t - start] if t >= start else base_loads[t]
            f = self._step(f, self._move(t, load))
        p = self.ctx.params
        assert p is not None
        return f.value(p.e0_kwh)

    def run(self, loads: list[float]) -> list[SlotExec]:
        """Play the optimal policy forward from the true starting energy."""
        ctx, p = self.ctx, self.ctx.params
        assert p is not None
        back = self.backward(loads)
        energy = p.e0_kwh
        out: list[SlotExec] = []
        for t in range(self.n):
            move, future = self._move(t, loads[t]), back[t + 1]
            candidates = {0.0, move.d_lo, move.d_hi, self.lo - energy, self.hi - energy}
            candidates.update(d for d in move.breakpoints())
            candidates.update(u - energy for u in future.arrays()[0])
            d_min = max(move.d_lo, self.lo - energy)
            d_max = min(move.d_hi, self.hi - energy)
            best_d, best_v = 0.0, move.value(0.0) + future.value(energy)
            for d in sorted((x for x in candidates if d_min - 1e-12 <= x <= d_max + 1e-12), key=abs):
                d = min(max(d, d_min), d_max)
                v = move.value(d) + future.value(energy + d)
                if v < best_v - _TIE:
                    best_d, best_v = d, v
            charge = best_d / (p.eta * ctx.dt) if best_d > 0 else 0.0
            discharge = -best_d * p.eta / ctx.dt if best_d < 0 else 0.0
            energy = min(max(energy + best_d, self.lo), self.hi)
            env = ctx.envs[t]
            out.append(SlotExec(loads[t], charge, discharge, resolve_flows(env, loads[t], charge, discharge), energy))
        return out
