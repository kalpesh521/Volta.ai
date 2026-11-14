"""
Static analysis of a condition tree: can it ever be true, is it always true?

WHY: an LLM happily writes `soc < 10 and soc > 90` (never fires) or `soc >= 0` (fires on every reading). Both are bugs
the user would only discover weeks later, so validation rejects them with a message that says what is wrong.

Method: every comparison becomes an interval set over its metric, clipped to the metric's plausible range. The tree is
converted to disjunctive normal form (a list of "boxes", each box being one interval set per metric), pushing `not`
down with De Morgan. A tree is unsatisfiable if no box is non-empty; it is a tautology if its negation is unsatisfiable.

Known limit: metrics are treated as independent. Physical couplings (grid import and export are never both positive;
SOC and battery temperature move together) are not modelled, so some impossible conditions slip through, but nothing
satisfiable is ever rejected.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.agents_platform.automation.dsl import AllOf, AnyOf, Compare, NotOf
from app.agents_platform.automation.metrics import MetricSpec, resolve_metric

_MAX_BOXES = 512
_INF = math.inf


class AnalysisTooComplex(Exception):
    """The DNF grew past the cap; callers fall back to 'assume satisfiable and not a tautology'."""


@dataclass(frozen=True)
class _Interval:
    lo: float
    hi: float
    lo_closed: bool
    hi_closed: bool

    def is_empty(self) -> bool:
        return self.lo > self.hi or (self.lo == self.hi and not (self.lo_closed and self.hi_closed))

    def contains(self, x: float) -> bool:
        above = x > self.lo or (x == self.lo and self.lo_closed)
        below = x < self.hi or (x == self.hi and self.hi_closed)
        return above and below


IntervalSet = tuple[_Interval, ...]


def _normalize(intervals: list[_Interval]) -> IntervalSet:
    live = sorted((i for i in intervals if not i.is_empty()), key=lambda i: (i.lo, not i.lo_closed))
    merged: list[_Interval] = []
    for current in live:
        if merged:
            last = merged[-1]
            touches = current.lo < last.hi or (current.lo == last.hi and (current.lo_closed or last.hi_closed))
            if touches:
                if current.hi > last.hi or (current.hi == last.hi and current.hi_closed):
                    merged[-1] = _Interval(last.lo, current.hi, last.lo_closed, current.hi_closed)
                continue
        merged.append(current)
    return tuple(merged)


def _intersect_sets(a: IntervalSet, b: IntervalSet) -> IntervalSet:
    out: list[_Interval] = []
    for x in a:
        for y in b:
            if x.lo > y.lo:
                lo, lo_closed = x.lo, x.lo_closed
            elif y.lo > x.lo:
                lo, lo_closed = y.lo, y.lo_closed
            else:
                lo, lo_closed = x.lo, x.lo_closed and y.lo_closed
            if x.hi < y.hi:
                hi, hi_closed = x.hi, x.hi_closed
            elif y.hi < x.hi:
                hi, hi_closed = y.hi, y.hi_closed
            else:
                hi, hi_closed = x.hi, x.hi_closed and y.hi_closed
            out.append(_Interval(lo, hi, lo_closed, hi_closed))
    return _normalize(out)


def _complement(s: IntervalSet) -> IntervalSet:
    gaps: list[_Interval] = []
    cursor, cursor_closed = -_INF, False  # the next gap starts here; closed if the previous interval ended open
    for interval in s:
        gaps.append(_Interval(cursor, interval.lo, cursor_closed, not interval.lo_closed))
        cursor, cursor_closed = interval.hi, not interval.hi_closed
    gaps.append(_Interval(cursor, _INF, cursor_closed, False))
    return _normalize(gaps)


def _from_compare(op: str, t: float) -> IntervalSet:
    if op == ">":
        return (_Interval(t, _INF, False, False),)
    if op == ">=":
        return (_Interval(t, _INF, True, False),)
    if op == "<":
        return (_Interval(-_INF, t, False, False),)
    if op == "<=":
        return (_Interval(-_INF, t, False, True),)
    if op == "==":
        return (_Interval(t, t, True, True),)
    return _normalize([_Interval(-_INF, t, False, False), _Interval(t, _INF, False, False)])  # "!="


def _domain(spec: MetricSpec) -> IntervalSet:
    return (_Interval(spec.min_value, spec.max_value, True, True),)


def _inhabited(s: IntervalSet, spec: MetricSpec) -> bool:
    """Non-empty; for 0/1 metrics only the points 0 and 1 count (so `!= 0 and != 1` is empty)."""
    if spec.discrete:
        return any(i.contains(0.0) or i.contains(1.0) for i in s)
    return bool(s)


Box = dict[str, IntervalSet]


def _conjoin(left: list[Box], right: list[Box]) -> list[Box]:
    out: list[Box] = []
    for a in left:
        for b in right:
            merged: Box = dict(a)
            viable = True
            for metric, s in b.items():
                combined = _intersect_sets(merged[metric], s) if metric in merged else s
                spec = resolve_metric(metric)
                if spec is None or not _inhabited(combined, spec):
                    viable = False
                    break
                merged[metric] = combined
            if viable:
                out.append(merged)
                if len(out) > _MAX_BOXES:
                    raise AnalysisTooComplex
    return out


def _dnf(node: Compare | AllOf | AnyOf | NotOf, negate: bool) -> list[Box]:
    if isinstance(node, Compare):
        spec = resolve_metric(node.metric)
        if spec is None:
            return [{}]  # unknown metric: already reported elsewhere; assume anything can happen
        domain = _domain(spec)
        truth = _intersect_sets(_from_compare(node.op, node.value), domain)
        chosen = _intersect_sets(_complement(truth), domain) if negate else truth
        return [{node.metric: chosen}] if _inhabited(chosen, spec) else []
    if isinstance(node, NotOf):
        return _dnf(node.condition, not negate)
    is_conjunction = isinstance(node, AllOf) != negate  # De Morgan: not(all) is any of the negations
    parts = [_dnf(child, negate) for child in node.conditions]
    if is_conjunction:
        result: list[Box] = [{}]
        for part in parts:
            result = _conjoin(result, part)
            if not result:
                break
        return result
    flat = [box for part in parts for box in part]
    if len(flat) > _MAX_BOXES:
        raise AnalysisTooComplex
    return flat


def is_unsatisfiable(node: Compare | AllOf | AnyOf | NotOf) -> bool:
    """True only when the condition provably can never hold (within metric ranges). False when unsure."""
    try:
        return not _dnf(node, negate=False)
    except AnalysisTooComplex:
        return False


def is_tautology(node: Compare | AllOf | AnyOf | NotOf) -> bool:
    """True only when the condition provably holds for every in-range value. False when unsure."""
    try:
        return not _dnf(node, negate=True)
    except AnalysisTooComplex:
        return False
