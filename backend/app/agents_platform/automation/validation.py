"""
`validate_rule`: the gate between "something an LLM or a user wrote" and "a rule the engine may run".

It never returns a half-valid rule and it never stops at the first problem: it raises `RuleValidationError` carrying a
LIST of `RuleProblem(code, path, message)` so the agent can repair everything in one retry. `code` is stable (tests and
prompts key on it), `path` locates the offending node (`trigger.conditions[1].value`), `message` is plain language.

Three layers, cheapest first: (1) a non-recursive prescan so a hostile 10,000-level JSON never reaches the recursive
parser; (2) the strict pydantic schema in `dsl.py`; (3) semantic checks that need the home's `ValidationContext`
(device allow-list, critical devices, ranges, contradictions, cooldown floor).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import ValidationError

from app.agents_platform.automation import analysis
from app.agents_platform.automation.dsl import (
    MAX_CONDITION_DEPTH,
    MAX_CONDITION_NODES,
    CodedValueError,
    Compare,
    NotifyAction,
    ProposeDeviceAction,
    Rule,
    children_of,
    referenced_metrics,
    walk,
)
from app.agents_platform.automation.metrics import device_key_of, resolve_metric
from app.agents_platform.automation.templates import TEMPLATES

MAX_PROBLEMS = 25
_UNION_TAGS = frozenset({"compare", "all", "any", "not", "notify", "propose_device_action"})
_ORDERING_OPS = frozenset({"<", "<=", ">", ">="})


@dataclass(frozen=True)
class RuleProblem:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class RuleValidationError(ValueError):
    def __init__(self, problems: Iterable[RuleProblem]) -> None:
        self.problems: list[RuleProblem] = list(problems)[:MAX_PROBLEMS]
        super().__init__("; ".join(f"[{p.code}] {p.path}: {p.message}" for p in self.problems))

    @property
    def codes(self) -> list[str]:
        return [p.code for p in self.problems]

    def to_dicts(self) -> list[dict[str, str]]:
        return [p.to_dict() for p in self.problems]


@dataclass(frozen=True)
class DeviceSpec:
    """One device a rule may mention. `critical` devices can never be switched off or set to 0 by a rule."""

    key: str
    critical: bool = False
    controllable: bool = True


@dataclass(frozen=True)
class ValidationContext:
    devices: Mapping[str, DeviceSpec] = field(default_factory=dict)
    existing_rule_ids: frozenset[str] = frozenset()  # for an edit, pass the set WITHOUT the rule being edited
    home_timezone: str | None = None
    battery_present: bool = True
    price_available: bool = False
    min_cooldown_seconds: int = 60
    max_fires_per_day_limit: int = 50

    @classmethod
    def with_devices(cls, devices: Iterable[DeviceSpec], **kwargs: Any) -> ValidationContext:
        return cls(devices={d.key: d for d in devices}, **kwargs)


# --- layer 1: prescan -----------------------------------------------------------------------------------------------


def _prescan(raw: Mapping[str, Any]) -> list[RuleProblem]:
    """Iterative depth/node count over untrusted input, stopping as soon as a limit is crossed."""
    stack: list[tuple[Any, int]] = [(raw.get("trigger"), 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        nodes += 1
        if depth > MAX_CONDITION_DEPTH:
            return [RuleProblem("condition_too_deep", "trigger",
                                f"conditions nest deeper than the limit of {MAX_CONDITION_DEPTH} levels")]
        if nodes > MAX_CONDITION_NODES:
            return [RuleProblem("condition_too_many_nodes", "trigger",
                                f"the condition has more than the limit of {MAX_CONDITION_NODES} nodes")]
        group = node.get("conditions")
        if isinstance(group, list):
            for child in group[: MAX_CONDITION_NODES + 1]:
                stack.append((child, depth + 1))
        single = node.get("condition")
        if isinstance(single, dict):
            stack.append((single, depth + 1))
    return []


# --- layer 2: schema errors -> problems -----------------------------------------------------------------------------

_PYDANTIC_CODES = {
    "missing": "missing_field",
    "extra_forbidden": "unknown_field",
    "too_short": "too_few_items",
    "too_long": "too_many_items",
    "greater_than": "value_out_of_bounds",
    "greater_than_equal": "value_out_of_bounds",
    "less_than": "value_out_of_bounds",
    "less_than_equal": "value_out_of_bounds",
    "literal_error": "invalid_choice",
    "union_tag_invalid": "invalid_kind",
    "union_tag_not_found": "invalid_kind",
    "string_pattern_mismatch": "invalid_format",
    "finite_number": "wrong_type",
}


def _format_path(loc: tuple[Any, ...]) -> str:
    path = ""
    previous: Any = None
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part in _UNION_TAGS and (isinstance(previous, int) or previous in {"trigger", "condition", None}):
            previous = part
            continue  # pydantic inserts the matched union tag into the location; it is noise for the reader
        else:
            path += f".{part}" if path else str(part)
        previous = part
    return path or "$"


def _problems_from_schema(error: ValidationError) -> list[RuleProblem]:
    problems: list[RuleProblem] = []
    for item in error.errors():
        original = (item.get("ctx") or {}).get("error")
        if isinstance(original, CodedValueError):
            code, message = original.code, original.message
        else:
            kind = item["type"]
            code = _PYDANTIC_CODES.get(kind, "wrong_type" if kind.endswith("_type") else "invalid_value")
            message = item["msg"]
        problems.append(RuleProblem(code, _format_path(item["loc"]), message))
    return problems


# --- layer 3: semantics ---------------------------------------------------------------------------------------------


def _check_conditions(rule: Rule, ctx: ValidationContext) -> list[RuleProblem]:
    problems: list[RuleProblem] = []
    for path, node in walk(rule.trigger):
        if not isinstance(node, Compare):
            continue
        spec = resolve_metric(node.metric, ctx.devices.keys())
        if spec is None:
            key = device_key_of(node.metric)
            problems.append(RuleProblem(
                "unknown_device", f"{path}.metric",
                f"device '{key}' is not one of this home's devices {sorted(ctx.devices)}",
            ))
            continue
        if (spec.needs == "battery" and not ctx.battery_present) or (spec.needs == "price" and not ctx.price_available):
            problems.append(RuleProblem(
                "metric_unavailable", f"{path}.metric", f"this home has no {spec.needs} data, so '{spec.id}' cannot be used"
            ))
            continue
        if spec.discrete:
            if node.op not in {"==", "!="} or node.value not in (0.0, 1.0):
                problems.append(RuleProblem(
                    "bool_metric_condition", path, f"'{spec.id}' is 0 or 1: compare it with == or != to 0 or 1"
                ))
            continue
        if not spec.in_range(node.value):
            problems.append(RuleProblem(
                "threshold_out_of_range", f"{path}.value",
                f"{node.value:g} is outside the plausible range for {spec.id} "
                f"({spec.min_value:g} to {spec.max_value:g} {spec.unit})",
            ))
    return problems


def _dead_branches(node: Any, path: str) -> list[RuleProblem]:
    """Report the highest unsatisfiable sub-trees below a satisfiable root."""
    found: list[RuleProblem] = []
    for suffix, child in children_of(node):
        child_path = path + suffix
        if analysis.is_unsatisfiable(child):
            found.append(RuleProblem(
                "condition_branch_never_true", child_path,
                "this part of the condition can never be true (its comparisons contradict each other)",
            ))
        else:
            found.extend(_dead_branches(child, child_path))
    return found


def _check_logic(rule: Rule) -> list[RuleProblem]:
    if analysis.is_unsatisfiable(rule.trigger):
        return [RuleProblem(
            "condition_never_true", "trigger",
            "the condition can never be true (for example 'soc < 10 and soc > 90'); the rule would never fire",
        )]
    if analysis.is_tautology(rule.trigger):
        return [RuleProblem(
            "condition_always_true", "trigger",
            "the condition is true for every possible reading, so it carries no information",
        )]
    return _dead_branches(rule.trigger, "trigger")


def _check_timing(rule: Rule, ctx: ValidationContext) -> list[RuleProblem]:
    problems: list[RuleProblem] = []
    if rule.cooldown_seconds < ctx.min_cooldown_seconds:
        problems.append(RuleProblem(
            "cooldown_too_short", "cooldown_seconds",
            f"cooldown is {rule.cooldown_seconds}s; at least {ctx.min_cooldown_seconds}s is required to prevent repeat storms",
        ))
    if rule.max_fires_per_day > ctx.max_fires_per_day_limit:
        problems.append(RuleProblem(
            "repetition_unbounded", "max_fires_per_day",
            f"max_fires_per_day is {rule.max_fires_per_day}; the limit is {ctx.max_fires_per_day_limit}",
        ))
    if rule.hysteresis > 0:
        usable = any(
            isinstance(node, Compare) and node.op in _ORDERING_OPS and (s := resolve_metric(node.metric)) is not None
            and s.supports_hysteresis
            for _, node in walk(rule.trigger)
        )
        if not usable:
            problems.append(RuleProblem(
                "hysteresis_ineffective", "hysteresis",
                "hysteresis only applies to <, <=, >, >= on numeric metrics; this condition has none, set it to 0",
            ))
    if rule.timezone != (ctx.home_timezone or rule.timezone):
        problems.append(RuleProblem(
            "timezone_mismatch", "timezone", f"the home's timezone is {ctx.home_timezone}, not {rule.timezone}"
        ))
    schedule = rule.schedule
    if schedule is not None and not schedule.windows and len(schedule.weekdays) == 7:
        problems.append(RuleProblem(
            "schedule_no_restriction", "schedule", "the schedule allows every day and hour; remove it or add a window or weekdays"
        ))
    return problems


def _check_actions(rule: Rule, ctx: ValidationContext) -> list[RuleProblem]:
    problems: list[RuleProblem] = []
    metrics = referenced_metrics(rule.trigger)
    proposals: dict[str, tuple[int, ProposeDeviceAction]] = {}
    for index, action in enumerate(rule.actions):
        path = f"actions[{index}]"
        if any(action == earlier for earlier in rule.actions[:index]):
            problems.append(RuleProblem("duplicate_action", path, "this action is listed twice"))
            continue
        if isinstance(action, NotifyAction):
            required = TEMPLATES[action.template].requires_metric
            if required and required not in metrics:
                problems.append(RuleProblem(
                    "template_metric_mismatch", f"{path}.template",
                    f"template '{action.template}' talks about {required}, which this condition never checks",
                ))
            continue
        device = ctx.devices.get(action.device)
        if device is None:
            problems.append(RuleProblem(
                "unknown_device", f"{path}.device", f"'{action.device}' is not one of this home's devices {sorted(ctx.devices)}"
            ))
            continue
        turns_off = action.action == "turn_off" or (action.action == "set_level" and action.level_percent == 0)
        if turns_off and device.critical:
            problems.append(RuleProblem(
                "critical_device_turn_off", path,
                f"'{device.key}' is a critical device and no rule may switch it off or set it to 0",
            ))
        if not device.controllable:
            problems.append(RuleProblem(
                "device_not_controllable", f"{path}.device", f"'{device.key}' cannot be controlled remotely"
            ))
        earlier_index = proposals.get(action.device)
        if earlier_index is not None:
            problems.append(RuleProblem(
                "conflicting_actions", path,
                f"actions[{earlier_index[0]}] already targets '{action.device}'; one device gets one proposal per rule",
            ))
        else:
            proposals[action.device] = (index, action)
    return problems


def validate_rule(raw: dict[str, Any], ctx: ValidationContext) -> Rule:
    """Parse and vet one rule. Returns the frozen `Rule` or raises `RuleValidationError` with every problem found."""
    if not isinstance(raw, dict):
        raise RuleValidationError([RuleProblem("not_an_object", "$", "a rule must be a JSON object")])
    early = _prescan(raw)
    if early:
        raise RuleValidationError(early)
    try:
        rule = Rule.model_validate(raw)
    except ValidationError as error:
        raise RuleValidationError(_problems_from_schema(error)) from None

    problems: list[RuleProblem] = []
    if rule.id in ctx.existing_rule_ids:
        problems.append(RuleProblem("duplicate_rule_id", "id", f"a rule with id '{rule.id}' already exists"))
    condition_problems = _check_conditions(rule, ctx)
    problems.extend(condition_problems)
    if not condition_problems:  # contradiction analysis is meaningless on leaves we already rejected
        problems.extend(_check_logic(rule))
    problems.extend(_check_timing(rule, ctx))
    problems.extend(_check_actions(rule, ctx))
    if problems:
        raise RuleValidationError(problems)
    return rule


def validate_rules(raws: list[dict[str, Any]], ctx: ValidationContext) -> list[Rule]:
    """Validate a batch, additionally rejecting duplicate ids inside the batch. Problem paths are prefixed `rules[i].`."""
    rules: list[Rule] = []
    problems: list[RuleProblem] = []
    seen: set[str] = set(ctx.existing_rule_ids)
    for index, raw in enumerate(raws):
        local_ctx = replace(ctx, existing_rule_ids=frozenset(seen))
        try:
            rule = validate_rule(raw, local_ctx)
        except RuleValidationError as error:
            problems.extend(RuleProblem(p.code, f"rules[{index}].{p.path}", p.message) for p in error.problems)
            continue
        seen.add(rule.id)
        rules.append(rule)
    if problems:
        raise RuleValidationError(problems)
    return rules
