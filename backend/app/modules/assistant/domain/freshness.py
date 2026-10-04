"""
Stale-data guard.

A recommendation built on old telemetry is worse than no recommendation, so
the workflow downgrades confidence and warns when data is older than
`AI_STALE_DATA_SECONDS`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


class DataFreshness(BaseModel):
    status: FreshnessStatus
    data_time: datetime | None = None
    age_seconds: int | None = None
    threshold_seconds: int
    message: str | None = None


def _humanize(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} minutes"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def assess_freshness(
    data_time: datetime | None,
    *,
    now: datetime,
    threshold_seconds: int,
) -> DataFreshness:
    if data_time is None:
        return DataFreshness(
            status=FreshnessStatus.MISSING,
            threshold_seconds=threshold_seconds,
            message="No telemetry is available for this home yet.",
        )
    stamp = data_time if data_time.tzinfo else data_time.replace(tzinfo=timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    # Clock skew or a fast-forward simulator can stamp ticks slightly ahead.
    age = max(0, int((current - stamp).total_seconds()))
    if age > threshold_seconds:
        return DataFreshness(
            status=FreshnessStatus.STALE,
            data_time=stamp,
            age_seconds=age,
            threshold_seconds=threshold_seconds,
            message=(
                f"The latest energy data is {_humanize(age)} old, so it may not reflect "
                "what is happening now. Treat this answer as indicative only."
            ),
        )
    return DataFreshness(
        status=FreshnessStatus.FRESH,
        data_time=stamp,
        age_seconds=age,
        threshold_seconds=threshold_seconds,
    )
