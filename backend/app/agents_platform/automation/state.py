"""
Per-rule runtime state. A plain dataclass of strings, numbers and None so `json.dumps(state.to_dict())` just works and
the worker can persist it in one JSON column between ticks.

Timestamps are UTC epoch seconds (floats), not datetimes: they serialise without a codec and compare without timezone
bugs. The daily counter is keyed by the home's local date string because "per day" means the household's day.

Phases (edge-triggered, see `evaluator.py` for the transitions):
    inactive  condition false (or not yet armed)
    pending   condition true, waiting out `for_seconds`
    firing    fired; stays here while the condition holds, which is what stops a second firing
    cooling   condition cleared but the cooldown from the last firing has not elapsed
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

STATE_VERSION = 1
INACTIVE = "inactive"
PENDING = "pending"
FIRING = "firing"
COOLING = "cooling"
PHASES = (INACTIVE, PENDING, FIRING, COOLING)


@dataclass(frozen=True)
class RuleState:
    phase: str = INACTIVE
    since: float | None = None  # pending: when the condition first became true (continuity-adjusted)
    last_true: float | None = None  # pending: last time the condition was positively known true
    fired_at: float | None = None  # firing/cooling: when the rule last fired
    cooling_until: float | None = None  # cooling: when the cooldown ends
    day: str | None = None  # local date (YYYY-MM-DD) the counters below belong to
    fires_today: int = 0
    suppressed_total: int = 0  # firings swallowed by max_fires_per_day, for observability
    version: int = STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuleState:
        """Strict load. A corrupt row raises ValueError; the caller decides whether to reset to `RuleState()`."""
        if not isinstance(data, dict):
            raise ValueError("rule state must be a JSON object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown rule-state keys: {sorted(unknown)}")
        state = cls(**data)
        if state.version != STATE_VERSION:
            raise ValueError(f"unsupported rule-state version {state.version}")
        if state.phase not in PHASES:
            raise ValueError(f"unknown rule-state phase {state.phase!r}")
        if state.phase == PENDING and (state.since is None or state.last_true is None):
            raise ValueError("pending state needs since and last_true")
        if state.phase in {FIRING, COOLING} and state.fired_at is None:
            raise ValueError(f"{state.phase} state needs fired_at")
        if state.phase == COOLING and state.cooling_until is None:
            raise ValueError("cooling state needs cooling_until")
        return state
