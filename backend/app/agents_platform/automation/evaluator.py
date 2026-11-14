"""
The rule evaluator: a pure, edge-triggered state machine. No clock, no I/O, no randomness; `now` is an argument.

    evaluate(rule, state, reading, now) -> (new_state, [Firing])

WHY edge-triggered: a rule such as "battery below 20%" is true for hours. Level-triggered evaluation would notify on
every tick. Here a rule fires once, when its condition becomes true and has held for `for_seconds`, and cannot fire
again until the condition has cleared AND the cooldown since the last firing has elapsed.

    inactive --enter--> pending --held for_seconds--> firing --cleared--> cooling --cooldown over--> inactive
        ^                  |  (condition drops: back to inactive)            |  (cooldown already over: straight back)
        +------------------+-------------------------------------------------+

Policies worth knowing (each has a test):

- Unknown is not false. Conditions use three-valued (Kleene) logic: a comparison on a missing metric is UNKNOWN, `all`
  is false if any child is false, `any` is true if any child is true, otherwise unknown propagates, `not` keeps unknown.
  UNKNOWN never starts or fires a rule. While pending or firing it HOLDS the state: a sensor blip must neither reset a
  nearly-complete timer nor falsely declare an incident over.
- But the timer does not run on faith. Continuity is only credited across gaps of at most `stale_limit_seconds` between
  readings where the condition was positively true; a pending rule that has been blind for longer is dropped, and a
  `for_seconds` window never spans a longer gap. A reading older than the limit counts as all-unknown.
- Hysteresis is a Schmitt trigger: a rule ENTERS on the plain thresholds, but once pending or firing it only LEAVES when
  the thresholds, widened by the band, are no longer met. `not` flips the direction of the widening.
- The schedule is part of the trigger: outside its window the trigger is false (and ends a firing).
- `max_fires_per_day` (local calendar day) swallows extra firings; the state still moves to `firing`, so there is no
  refire storm once the cap lifts at midnight while the condition still holds.
- Disabled rules reset to a fresh state and never fire.

Known limits: `now` is assumed non-decreasing per rule (a clock stepping back only delays things, never double-fires,
because cooldown and pending arithmetic clamp at zero); the cooldown is measured from firing time, not from clearing.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.agents_platform.automation.dsl import (
    Action,
    AllOf,
    AnyOf,
    Compare,
    NotOf,
    Rule,
    referenced_metrics,
    rule_fingerprint,
)
from app.agents_platform.automation.metrics import read_metric, resolve_metric
from app.agents_platform.automation.readings import Reading, ensure_utc
from app.agents_platform.automation.state import COOLING, FIRING, INACTIVE, PENDING, RuleState

Tri = bool | None  # True, False or None (unknown)
_EPSILON = 1e-9


@dataclass(frozen=True)
class EvalConfig:
    # Matches the device-action freshness limit (600 s). Readings older than this, and gaps longer than this between
    # confirming readings, are not trusted to prove continuity.
    stale_limit_seconds: float = 600.0


DEFAULT_CONFIG = EvalConfig()


@dataclass(frozen=True)
class Firing:
    """One rule firing. The caller executes `actions` (notify / propose); the engine itself performs nothing."""

    rule_id: str
    fingerprint: str  # lets the executor prove the rule is the one the user approved
    fired_at: datetime
    actions: tuple[Action, ...]
    observed: Mapping[str, float | None]  # metric values behind the decision, for the audit trail


@lru_cache(maxsize=64)
def zone_for(name: str) -> ZoneInfo:
    return ZoneInfo(name)


# --- condition evaluation -------------------------------------------------------------------------------------------


def _band(metric: str, threshold: float, hysteresis: float) -> float:
    spec = resolve_metric(metric)
    if hysteresis <= 0 or spec is None or not spec.supports_hysteresis:
        return 0.0
    return max(hysteresis * abs(threshold), spec.min_band)


def _compare(op: str, left: float, right: float) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return math.isclose(left, right, rel_tol=0.0, abs_tol=_EPSILON)
    return not math.isclose(left, right, rel_tol=0.0, abs_tol=_EPSILON)


def eval_condition(
    node: Compare | AllOf | AnyOf | NotOf,
    read: Callable[[str], float | None],
    hysteresis: float = 0.0,
    widen: int = 0,
) -> Tri:
    """
    Three-valued evaluation. `widen` is +1 to make the condition easier to satisfy by the hysteresis band (used while
    a rule is already active), -1 to make it harder (what `not` turns +1 into), 0 for the plain thresholds.
    Equality leaves are never widened.
    """
    if isinstance(node, Compare):
        value = read(node.metric)
        if value is None:
            return None
        threshold = node.value
        if widen and node.op in {"<", "<="}:
            threshold += widen * _band(node.metric, node.value, hysteresis)
        elif widen and node.op in {">", ">="}:
            threshold -= widen * _band(node.metric, node.value, hysteresis)
        return _compare(node.op, value, threshold)
    if isinstance(node, NotOf):
        inner = eval_condition(node.condition, read, hysteresis, -widen)
        return None if inner is None else not inner
    results = [eval_condition(child, read, hysteresis, widen) for child in node.conditions]
    if isinstance(node, AllOf):
        if any(r is False for r in results):
            return False
        return None if any(r is None for r in results) else True
    if any(r is True for r in results):
        return True
    return None if any(r is None for r in results) else False


# --- state machine --------------------------------------------------------------------------------------------------


def _fire(rule: Rule, state: RuleState, t: float, now: datetime, observed: Mapping[str, float | None]) -> tuple[RuleState, list[Firing]]:
    if state.fires_today >= rule.max_fires_per_day:
        return (
            replace(state, phase=FIRING, fired_at=t, since=None, last_true=None, suppressed_total=state.suppressed_total + 1),
            [],
        )
    new_state = replace(state, phase=FIRING, fired_at=t, since=None, last_true=None, fires_today=state.fires_today + 1)
    firing = Firing(rule.id, rule_fingerprint(rule), now, tuple(rule.actions), dict(observed))
    return new_state, [firing]


def evaluate(
    rule: Rule,
    state: RuleState,
    reading: Reading,
    now: datetime,
    config: EvalConfig = DEFAULT_CONFIG,
) -> tuple[RuleState, list[Firing]]:
    """Advance one rule by one tick. Never raises on missing or odd data; returns the new state and any firings."""
    if not rule.enabled:
        return RuleState(), []

    now = ensure_utc(now)
    t = now.timestamp()
    local_now = now.astimezone(zone_for(rule.timezone))

    day = local_now.date().isoformat()
    if state.day != day:
        state = replace(state, day=day, fires_today=0)
    if state.phase == COOLING and t >= (state.cooling_until if state.cooling_until is not None else t):
        state = replace(state, phase=INACTIVE, cooling_until=None, since=None, last_true=None)

    stale = (t - reading.timestamp.timestamp()) > config.stale_limit_seconds
    usable = Reading(timestamp=reading.timestamp) if stale else reading  # a stale tick knows only the clock

    def read(metric: str) -> float | None:
        return read_metric(metric, usable, local_now)

    in_schedule = rule.schedule is None or rule.schedule.contains(local_now)
    if in_schedule:
        enter = eval_condition(rule.trigger, read)
        hold = eval_condition(rule.trigger, read, rule.hysteresis, +1) if rule.hysteresis > 0 else enter
    else:
        enter = hold = False

    phase = state.phase
    if phase == INACTIVE:
        if enter is True:
            if rule.for_seconds == 0:
                return _fire(rule, state, t, now, _observe(rule, read))
            return replace(state, phase=PENDING, since=t, last_true=t), []
        return state, []

    if phase == PENDING:
        since = state.since if state.since is not None else t
        last_true = state.last_true if state.last_true is not None else since
        if hold is False:
            return replace(state, phase=INACTIVE, since=None, last_true=None), []
        if hold is None:
            if t - last_true > config.stale_limit_seconds:
                return replace(state, phase=INACTIVE, since=None, last_true=None), []
            return state, []
        if t - last_true > config.stale_limit_seconds:
            since = t  # continuity across the gap is unproven: restart the timer
        if t - since >= rule.for_seconds:
            return _fire(rule, state, t, now, _observe(rule, read))
        return replace(state, since=since, last_true=t), []

    if phase == FIRING:
        if hold is False:
            until = (state.fired_at if state.fired_at is not None else t) + rule.cooldown_seconds
            if t < until:
                return replace(state, phase=COOLING, cooling_until=until), []
            return replace(state, phase=INACTIVE, cooling_until=None), []
        return state, []

    return state, []  # COOLING and the cooldown has not elapsed: nothing can happen yet


def _observe(rule: Rule, read: Callable[[str], float | None]) -> dict[str, float | None]:
    return {metric: read(metric) for metric in sorted(referenced_metrics(rule.trigger))}


def evaluate_all(
    rules: Iterable[Rule],
    states: Mapping[str, RuleState],
    reading: Reading,
    now: datetime,
    config: EvalConfig = DEFAULT_CONFIG,
) -> tuple[dict[str, RuleState], list[Firing]]:
    """
    Evaluate every rule against one reading. Returns the new state per rule id (rules missing from `states` start fresh;
    states of rules no longer supplied are dropped) and all firings in rule order.
    """
    new_states: dict[str, RuleState] = {}
    firings: list[Firing] = []
    for rule in rules:
        new_state, fired = evaluate(rule, states.get(rule.id, RuleState()), reading, now, config)
        new_states[rule.id] = new_state
        firings.extend(fired)
    return new_states, firings
