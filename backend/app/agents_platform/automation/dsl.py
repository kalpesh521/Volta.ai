"""
The automation rule DSL (version 1.0): small, closed, strict, JSON round-trippable.

WHY this shape: the Phase 4 agent compiles natural language into it, a human approves it, and a deterministic engine
(no LLM) runs it unattended against a real home. So every degree of freedom is a liability:

- unknown keys are rejected (`extra="forbid"`), types are strict (the string "20" is not a threshold);
- conditions are a bounded tree over a closed metric registry (`metrics.py`);
- actions are a closed set that can only notify or PROPOSE a device change, the existing confirm-before-act workflow
  still decides; there is no `set_flag` and no free text anywhere that reaches a user's phone;
- models are frozen, so an approved rule cannot be mutated behind the fingerprint's back.

Context-dependent checks (device allow-list, critical devices, contradictions, cooldown floor) live in `validation.py`
because they need a `ValidationContext`. Structural limits are enforced here too so no code path can build an oversized rule.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterator
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents_platform.automation.metrics import resolve_metric
from app.agents_platform.automation.templates import TEMPLATES

DSL_VERSION = "1.0"
MAX_CONDITION_DEPTH = 4  # a lone comparison has depth 1
MAX_CONDITION_NODES = 12  # comparisons and combinators together
MAX_ACTIONS = 3
MAX_SCHEDULE_WINDOWS = 4
DEFAULT_TIMEZONE = "Asia/Kolkata"

Operator = Literal[">", ">=", "<", "<=", "==", "!="]
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
_NAME_PUNCTUATION = frozenset(" -_.,:;()'/&%+!?")


def _name_char_ok(char: str) -> bool:
    """Letters, marks (Devanagari vowel signs are marks), digits and a little punctuation: no controls, braces or quotes."""
    return char in _NAME_PUNCTUATION or unicodedata.category(char)[0] in {"L", "M", "N"}


class CodedValueError(ValueError):
    """A validation failure with a stable machine code; `validation.py` surfaces `code` to the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# --- Conditions -----------------------------------------------------------------------------------------------------


class Compare(StrictModel):
    kind: Literal["compare"] = "compare"
    metric: str
    op: Operator
    value: float = Field(allow_inf_nan=False)

    @field_validator("metric")
    @classmethod
    def _metric_in_vocabulary(cls, metric: str) -> str:
        if resolve_metric(metric) is None:
            raise CodedValueError("unknown_metric", f"'{metric}' is not a metric the engine knows")
        return metric


class AllOf(StrictModel):
    kind: Literal["all"] = "all"
    conditions: list[Condition] = Field(min_length=2, max_length=MAX_CONDITION_NODES)


class AnyOf(StrictModel):
    kind: Literal["any"] = "any"
    conditions: list[Condition] = Field(min_length=2, max_length=MAX_CONDITION_NODES)


class NotOf(StrictModel):
    kind: Literal["not"] = "not"
    condition: Condition


Condition = Annotated[Union[Compare, AllOf, AnyOf, NotOf], Field(discriminator="kind")]


def children_of(node: Compare | AllOf | AnyOf | NotOf) -> list[tuple[str, Compare | AllOf | AnyOf | NotOf]]:
    """Direct children with their path suffix, in order."""
    if isinstance(node, (AllOf, AnyOf)):
        return [(f".conditions[{i}]", child) for i, child in enumerate(node.conditions)]
    if isinstance(node, NotOf):
        return [(".condition", node.condition)]
    return []


def walk(node: Compare | AllOf | AnyOf | NotOf, path: str = "trigger") -> Iterator[tuple[str, Compare | AllOf | AnyOf | NotOf]]:
    """Pre-order traversal yielding (path, node)."""
    yield path, node
    for suffix, child in children_of(node):
        yield from walk(child, path + suffix)


def condition_stats(node: Compare | AllOf | AnyOf | NotOf) -> tuple[int, int]:
    """(depth, node_count). Depth of a lone comparison is 1."""
    child_stats = [condition_stats(child) for _, child in children_of(node)]
    depth = 1 + max((d for d, _ in child_stats), default=0)
    return depth, 1 + sum(n for _, n in child_stats)


def referenced_metrics(node: Compare | AllOf | AnyOf | NotOf) -> frozenset[str]:
    return frozenset(n.metric for _, n in walk(node) if isinstance(n, Compare))


# --- Actions --------------------------------------------------------------------------------------------------------


class NotifyAction(StrictModel):
    kind: Literal["notify"] = "notify"
    channel: Literal["app", "push"]
    template: str
    params: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _template_and_params(self) -> NotifyAction:
        spec = TEMPLATES.get(self.template)
        if spec is None:
            raise CodedValueError(
                "unknown_template", f"template '{self.template}' does not exist; use one of {sorted(TEMPLATES)}"
            )
        expected = {p.name: p for p in spec.params}
        if set(self.params) != set(expected):
            raise CodedValueError(
                "template_params_invalid",
                f"template '{spec.id}' takes exactly these params: {sorted(expected) or 'none'}; got {sorted(self.params)}",
            )
        for name, value in self.params.items():
            bound = expected[name]
            if value != value or not bound.min_value <= value <= bound.max_value:
                raise CodedValueError(
                    "template_param_out_of_range",
                    f"param '{name}' must be between {bound.min_value:g} and {bound.max_value:g} {bound.unit}",
                )
        return self


class ProposeDeviceAction(StrictModel):
    """Only ever a PROPOSAL: the confirm-before-act workflow (safety checks, user approval) decides."""

    kind: Literal["propose_device_action"] = "propose_device_action"
    action: Literal["turn_on", "turn_off", "set_level"]
    device: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    level_percent: float | None = Field(default=None, ge=0, le=100)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode="after")
    def _params_match_action(self) -> ProposeDeviceAction:
        if self.action == "set_level" and self.level_percent is None:
            raise CodedValueError("param_required", "set_level needs level_percent (0-100)")
        if self.action != "set_level" and self.level_percent is not None:
            raise CodedValueError("param_not_applicable", f"level_percent only applies to set_level, not {self.action}")
        if self.action == "turn_off" and self.duration_minutes is not None:
            raise CodedValueError("param_not_applicable", "duration_minutes does not apply to turn_off")
        return self


Action = Annotated[Union[NotifyAction, ProposeDeviceAction], Field(discriminator="kind")]


# --- Schedule -------------------------------------------------------------------------------------------------------


class TimeWindow(StrictModel):
    """Local time window [start, end). `start > end` wraps midnight (22:00-06:00)."""

    start: str = Field(pattern=_HHMM)
    end: str = Field(pattern=_HHMM)

    @property
    def start_minute(self) -> int:
        return int(self.start[:2]) * 60 + int(self.start[3:])

    @property
    def end_minute(self) -> int:
        return int(self.end[:2]) * 60 + int(self.end[3:])

    @model_validator(mode="after")
    def _non_empty(self) -> TimeWindow:
        if self.start == self.end:
            raise CodedValueError(
                "schedule_window_empty", "a window needs different start and end; omit windows to allow the whole day"
            )
        return self


class Schedule(StrictModel):
    """
    When the rule is allowed to be active, in the rule's timezone. Outside the schedule the trigger counts as false,
    so leaving the window ends a firing and entering it with the condition already true starts one.
    Weekdays refer to the day a window STARTS on (a Monday 22:00-06:00 window includes Tuesday 02:00).
    """

    windows: list[TimeWindow] = Field(default_factory=list, max_length=MAX_SCHEDULE_WINDOWS)
    weekdays: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=lambda: list(WEEKDAYS), min_length=1, max_length=7
    )

    @field_validator("weekdays")
    @classmethod
    def _canonical_weekdays(cls, days: list[str]) -> list[str]:
        # Sorted and unique so that two spellings of the same schedule fingerprint identically.
        return [d for d in WEEKDAYS if d in set(days)]

    def contains(self, local_now) -> bool:  # noqa: ANN001 - any aware/naive datetime already in local time
        minute = local_now.hour * 60 + local_now.minute
        today = local_now.weekday()
        days = {WEEKDAYS.index(d) for d in self.weekdays}
        if not self.windows:
            return today in days
        for window in self.windows:
            start, end = window.start_minute, window.end_minute
            if start < end:
                if start <= minute < end and today in days:
                    return True
            else:  # wraps midnight: the evening part belongs to today, the morning part to yesterday's window
                if minute >= start and today in days:
                    return True
                if minute < end and (today - 1) % 7 in days:
                    return True
        return False


# --- Rule -----------------------------------------------------------------------------------------------------------


class Rule(StrictModel):
    dsl_version: Literal["1.0"] = DSL_VERSION
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE
    trigger: Condition
    # The condition must hold continuously this long before the rule fires (0 = fire on the first true reading).
    for_seconds: int = Field(default=0, ge=0, le=86_400)
    # After firing, the rule cannot fire again for this long, even if it cleared and re-triggered meanwhile.
    cooldown_seconds: int = Field(default=900, ge=0, le=2_592_000)
    # Relative margin (0-0.5 of each threshold's magnitude, with a per-metric floor) by which a comparison stays
    # satisfied after it fired, so a value hovering at the threshold does not clear and re-arm repeatedly.
    hysteresis: float = Field(default=0.0, ge=0.0, le=0.5, allow_inf_nan=False)
    # Hard cap on firings per local calendar day: bounded repetition even if everything else is mis-tuned.
    max_fires_per_day: int = Field(default=10, ge=1, le=1000)
    schedule: Schedule | None = None
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)

    @field_validator("name")
    @classmethod
    def _plain_name(cls, name: str) -> str:
        if name != name.strip() or not all(_name_char_ok(c) for c in name):
            raise CodedValueError(
                "name_invalid",
                "name must be plain text (letters, digits, spaces and . , : ; ( ) ' / & % + ! ? -) "
                "without leading or trailing spaces or line breaks",
            )
        return name

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, tz: str) -> str:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise CodedValueError("timezone_invalid", f"'{tz}' is not an IANA timezone like 'Asia/Kolkata'") from None
        return tz

    @model_validator(mode="after")
    def _structural_limits(self) -> Rule:
        depth, nodes = condition_stats(self.trigger)
        if depth > MAX_CONDITION_DEPTH:
            raise CodedValueError(
                "condition_too_deep", f"conditions nest {depth} levels deep; the limit is {MAX_CONDITION_DEPTH}"
            )
        if nodes > MAX_CONDITION_NODES:
            raise CodedValueError(
                "condition_too_many_nodes", f"the condition has {nodes} nodes; the limit is {MAX_CONDITION_NODES}"
            )
        return self


Compare.model_rebuild()
AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()
Rule.model_rebuild()


def rule_fingerprint(rule: Rule) -> str:
    """
    Stable SHA-256 of what the rule DOES, so an approved rule can be proven unchanged at execution time.

    Canonical JSON (sorted keys, no whitespace) over the whole rule except `enabled`: pausing and resuming an approved
    rule must not demand re-approval, anything else (name, thresholds, actions, schedule, ids) must. The order of
    children in `all`/`any` is part of the identity; the fingerprint does not try to prove two reorderings equivalent.
    """
    payload = rule.model_dump(mode="json", exclude={"enabled"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"volta-rule-dsl/{rule.dsl_version}\n{canonical}".encode()).hexdigest()
