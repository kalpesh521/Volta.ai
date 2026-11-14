"""
Dry run: replay a rule over historical readings and report when it WOULD have fired.

WHY: before a user approves an automation they should see what it would have done last week ("it would have notified you
11 times on Tuesday") instead of finding out the first noisy night. The replay uses the real `evaluate`, with `now` set to
each reading's own timestamp, so there is no second implementation to drift. Nothing is executed.

Known limits: it can only be as good as the history it is given (gaps in telemetry look like unknown data, which the
evaluator treats conservatively); price and device metrics are only replayed if the readings carry them; the stale-data
limit is widened for sparse history (see `_default_config`), so a for_seconds rule is judged on the history's own cadence,
which may differ from the live tick rate.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any

from app.agents_platform.automation.dsl import Rule
from app.agents_platform.automation.evaluator import DEFAULT_CONFIG, EvalConfig, eval_condition, evaluate, zone_for
from app.agents_platform.automation.metrics import read_metric
from app.agents_platform.automation.readings import Reading
from app.agents_platform.automation.state import PENDING, RuleState

DEFAULT_NOISY_PER_DAY = 3


@dataclass(frozen=True)
class DryRunReport:
    rule_id: str
    readings_used: int
    fired_at: list[datetime]
    per_day: dict[str, int]  # local date in the rule's timezone -> firings
    longest_pending_seconds: float
    suppressed_count: int  # firings swallowed by max_fires_per_day
    unknown_readings: int  # readings on which the trigger could not be decided
    stale_limit_seconds: float
    noisy_threshold_per_day: int = DEFAULT_NOISY_PER_DAY
    min_gap_seconds: float | None = None
    span_days: float = 0.0

    @property
    def per_day_max(self) -> int:
        return max(self.per_day.values(), default=0)

    @property
    def noisy(self) -> bool:
        return self.per_day_max > self.noisy_threshold_per_day

    def summary(self) -> dict[str, Any]:
        """JSON-ready facts for the agent to turn into plain language. Counts, never prose."""
        return {
            "rule_id": self.rule_id,
            "fired_count": len(self.fired_at),
            "first_fired_at": self.fired_at[0].isoformat() if self.fired_at else None,
            "last_fired_at": self.fired_at[-1].isoformat() if self.fired_at else None,
            "per_day_max": self.per_day_max,
            "per_day": dict(sorted(self.per_day.items())),
            "noisy": self.noisy,
            "noisy_threshold_per_day": self.noisy_threshold_per_day,
            "longest_pending_seconds": self.longest_pending_seconds,
            "min_gap_between_fires_seconds": self.min_gap_seconds,
            "suppressed_by_daily_cap": self.suppressed_count,
            "readings_used": self.readings_used,
            "readings_undecidable": self.unknown_readings,
            "history_days": round(self.span_days, 2),
        }


def _default_config(readings: Sequence[Reading]) -> EvalConfig:
    steps = [b.timestamp.timestamp() - a.timestamp.timestamp() for a, b in zip(readings, readings[1:])]
    steps = [s for s in steps if s > 0]
    if not steps:
        return DEFAULT_CONFIG
    return EvalConfig(stale_limit_seconds=max(DEFAULT_CONFIG.stale_limit_seconds, 2.0 * median(steps)))


def dry_run(
    rule: Rule,
    readings: Sequence[Reading],
    *,
    noisy_threshold_per_day: int = DEFAULT_NOISY_PER_DAY,
    config: EvalConfig | None = None,
) -> DryRunReport:
    """Replay `rule` over `readings` (sorted by time here, so a newest-first DB query is fine). Rule `enabled` is ignored."""
    ordered = sorted(readings, key=lambda r: r.timestamp)
    config = config or _default_config(ordered)
    live_rule = rule if rule.enabled else rule.model_copy(update={"enabled": True})

    state = RuleState()
    fired_at: list[datetime] = []
    longest_pending = 0.0
    unknown = 0
    zone = zone_for(rule.timezone)

    for reading in ordered:
        now = reading.timestamp
        previous = state
        state, fired = evaluate(live_rule, state, reading, now, config)
        fired_at.extend(f.fired_at for f in fired)

        t = now.timestamp()
        if previous.phase == PENDING and previous.since is not None:
            if state.phase == PENDING and state.since is not None:
                longest_pending = max(longest_pending, t - state.since)
            elif fired:
                longest_pending = max(longest_pending, t - previous.since)
            elif previous.last_true is not None:
                longest_pending = max(longest_pending, previous.last_true - previous.since)
        elif state.phase == PENDING and state.since is not None:
            longest_pending = max(longest_pending, t - state.since)

        if _undecidable(live_rule, reading, now, config, zone):
            unknown += 1

    per_day = Counter(ts.astimezone(zone).date().isoformat() for ts in fired_at)
    gaps = [b.timestamp() - a.timestamp() for a, b in zip(fired_at, fired_at[1:])]
    span = (ordered[-1].timestamp - ordered[0].timestamp).total_seconds() / 86_400 if len(ordered) > 1 else 0.0
    return DryRunReport(
        rule_id=rule.id,
        readings_used=len(ordered),
        fired_at=fired_at,
        per_day=dict(per_day),
        longest_pending_seconds=longest_pending,
        suppressed_count=state.suppressed_total,
        unknown_readings=unknown,
        stale_limit_seconds=config.stale_limit_seconds,
        noisy_threshold_per_day=noisy_threshold_per_day,
        min_gap_seconds=min(gaps) if gaps else None,
        span_days=span,
    )


def _undecidable(rule: Rule, reading: Reading, now: datetime, config: EvalConfig, zone: Any) -> bool:
    local_now = now.astimezone(zone)
    return eval_condition(rule.trigger, lambda m: read_metric(m, reading, local_now)) is None
