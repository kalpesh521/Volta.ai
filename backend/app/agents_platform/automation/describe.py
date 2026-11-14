"""
Plain-language description of a rule, built from the DSL and nothing else.

What the user is asked to approve must be what the rule does, so the description is generated from the validated rule
by fixed code, never by a model. The same text is shown on the approval card and stored with the proposal.
"""
from __future__ import annotations

from collections.abc import Mapping

from app.agents_platform.automation.dsl import AllOf, AnyOf, Compare, Condition, NotifyAction, NotOf, ProposeDeviceAction, Rule
from app.agents_platform.automation.metrics import resolve_metric

_OPS = {">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", "==": "equals", "!=": "is not"}
_ACTION_WORDS = {"turn_on": "turn on", "turn_off": "turn off", "set_level": "set"}


_BOOLEAN_LABELS = {"grid.available": ("the grid supply", "available", "not available")}
_COMPARE = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _describe_boolean(condition: Compare, *, label: tuple[str, str, str] | None) -> str:
    """A 0/1 metric reads as a state ("the grid supply is not available"), whichever way the comparison is written."""
    true_for = {v for v in (0, 1) if _COMPARE[condition.op](v, condition.value)}
    subject, yes, no = label or (condition.metric, "true", "false")
    if true_for == {1}:
        return f"{subject} is {yes}"
    if true_for == {0}:
        return f"{subject} is {no}"
    return f"{subject} is {yes} or {no}" if true_for else f"{subject} is never {yes}"


def _number(value: float) -> str:
    return f"{value:g}"


def describe_condition(condition: Condition) -> str:
    if isinstance(condition, Compare):
        spec = resolve_metric(condition.metric)
        label = (spec.description if spec else condition.metric).rstrip(".").lower()
        unit = f" {spec.unit}" if spec and spec.unit and spec.unit != "bool" else ""
        if spec is not None and spec.unit == "bool":
            return _describe_boolean(condition, label=_BOOLEAN_LABELS.get(condition.metric))
        return f"{label} {_OPS[condition.op]} {_number(condition.value)}{unit}"
    if isinstance(condition, AllOf):
        return " and ".join(f"({describe_condition(c)})" if isinstance(c, (AllOf, AnyOf)) else describe_condition(c) for c in condition.conditions)
    if isinstance(condition, AnyOf):
        return " or ".join(f"({describe_condition(c)})" if isinstance(c, (AllOf, AnyOf)) else describe_condition(c) for c in condition.conditions)
    if isinstance(condition, NotOf):
        return f"not ({describe_condition(condition.condition)})"
    raise TypeError(f"unknown condition {type(condition).__name__}")  # pragma: no cover


def describe_action(action, device_names: Mapping[str, str]) -> str:
    if isinstance(action, NotifyAction):
        return f"send you a {'push ' if action.channel == 'push' else ''}notification"
    if isinstance(action, ProposeDeviceAction):
        name = device_names.get(action.device, action.device.replace("_", " "))
        extra = f" to {_number(action.level_percent)}%" if action.level_percent is not None else ""
        extra += f" for {action.duration_minutes} minutes" if action.duration_minutes else ""
        return f"ask you to confirm: {_ACTION_WORDS[action.action]} the {name}{extra}"
    raise TypeError(f"unknown action {type(action).__name__}")  # pragma: no cover


def describe_rule(rule: Rule, device_names: Mapping[str, str] | None = None) -> str:
    names = device_names or {}
    when = describe_condition(rule.trigger)
    hold = f", and it stays that way for {rule.for_seconds // 60 or rule.for_seconds} {'minutes' if rule.for_seconds >= 60 else 'seconds'}" if rule.for_seconds else ""
    then = "; ".join(describe_action(a, names) for a in rule.actions)
    again = f" It will not repeat within {rule.cooldown_seconds // 60} minutes and at most {rule.max_fires_per_day} times a day."
    return f"When {when}{hold}, the assistant will {then}.{again}"
