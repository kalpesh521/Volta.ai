"""
Home load forecast: a per-hour-of-day profile learnt from telemetry, with honest fallbacks.

    profile   >= 2 days of data covering >= 18 hours of the day. For each local hour, samples are
              averaged per calendar day, then smoothed across days with an EWMA (recent days weigh
              more: habits drift, a guest leaves). Weekday/weekend cells are used only when BOTH
              day-types have >= 3 days for that hour; otherwise the all-days cell is used.
    flat      latest reading (mean of the last hour) held constant, if it is fresh (<= 3 h old).
    unknown   nothing usable. The forecast says so; it never invents a load.

The spread of each hour comes from the day-to-day values actually observed (p10/p90), with floors
so a few suspiciously identical days cannot produce a zero-width interval, and is widened with lead
time by the caller-visible `horizon_widening`.

Known limits: no temperature/occupancy/appliance model (an AC heatwave day is invisible until it
shows up in the recent days), no holiday calendar, weekend days default to Sat/Sun.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Literal

from ._stats import quantile
from .models import HistoryPoint

LoadMethod = Literal["profile", "flat", "unknown"]

EWMA_ALPHA = 0.35
MIN_PROFILE_DAYS = 2
MIN_PROFILE_HOURS = 18
MIN_DAYS_PER_DAYTYPE_CELL = 3
MIN_DAYS_FOR_EMPIRICAL_SPREAD = 4
FLAT_MAX_AGE = timedelta(hours=3)
_DEFAULT_REL_SPREAD_PROFILE = 0.25
_DEFAULT_REL_SPREAD_FLAT = 0.40
_MIN_REL_HALF_WIDTH = 0.10
_WEEKEND_DEFAULT = frozenset({5, 6})


@dataclass(frozen=True, slots=True)
class LoadEstimate:
    p10_kw: float
    p50_kw: float
    p90_kw: float


@dataclass(frozen=True, slots=True)
class _Cell:
    value_kw: float
    p10_kw: float
    p90_kw: float
    days: int


def horizon_widening(lead_hours: float) -> float:
    """Interval half-width multiplier: 1.0 now, 1.25 at 24 h, 1.5 at 48 h."""
    return 1.0 + max(0.0, lead_hours) / 96.0


def _ewma(values: Sequence[float]) -> float:
    acc = values[0]
    for v in values[1:]:
        acc = EWMA_ALPHA * v + (1.0 - EWMA_ALPHA) * acc
    return acc


def _cell(values: Sequence[float]) -> _Cell:
    centre = _ewma(values)
    if len(values) >= MIN_DAYS_FOR_EMPIRICAL_SPREAD:
        lo, hi = quantile(values, 0.10), quantile(values, 0.90)
    else:
        lo, hi = centre * (1 - _DEFAULT_REL_SPREAD_PROFILE), centre * (1 + _DEFAULT_REL_SPREAD_PROFILE)
    lo = min(lo, centre * (1 - _MIN_REL_HALF_WIDTH))
    hi = max(hi, centre * (1 + _MIN_REL_HALF_WIDTH))
    return _Cell(centre, max(0.0, lo), hi, len(values))


@dataclass(frozen=True)
class LoadModel:
    method: LoadMethod
    tz: tzinfo
    points_used: int = 0
    days: int = 0
    flat_kw: float | None = None
    missing_hours: int = 0
    weekend_days: frozenset[int] = _WEEKEND_DEFAULT
    note: str = ""
    _all: dict[int, _Cell] = field(default_factory=dict)
    _typed: dict[tuple[bool, int], _Cell] = field(default_factory=dict)

    @property
    def uses_daytype_split(self) -> bool:
        return bool(self._typed)

    def predict(self, local_time: datetime, lead_hours: float) -> LoadEstimate | None:
        if self.method == "unknown":
            return None
        widen = horizon_widening(lead_hours)
        if self.method == "flat":
            assert self.flat_kw is not None
            centre = self.flat_kw
            half = centre * _DEFAULT_REL_SPREAD_FLAT * widen
            return LoadEstimate(max(0.0, centre - half), centre, centre + half)
        hour = local_time.astimezone(self.tz).hour
        weekend = local_time.astimezone(self.tz).weekday() in self.weekend_days
        cell = self._typed.get((weekend, hour)) or self._all.get(hour)
        if cell is None:
            # Hour never observed: fall back to the profile mean, with the flat (wider) spread.
            centre = sum(c.value_kw for c in self._all.values()) / len(self._all)
            half = centre * _DEFAULT_REL_SPREAD_FLAT * widen
            return LoadEstimate(max(0.0, centre - half), centre, centre + half)
        centre = cell.value_kw
        return LoadEstimate(
            max(0.0, centre - (centre - cell.p10_kw) * widen),
            centre,
            centre + (cell.p90_kw - centre) * widen,
        )


def build_load_model(
    history: Sequence[HistoryPoint],
    *,
    now: datetime,
    tz: tzinfo,
    weekend_days: frozenset[int] = _WEEKEND_DEFAULT,
) -> LoadModel:
    """Fit the best available load model. `history` should already be cleaned (finite, non-negative,
    not in the future)."""
    samples = [p for p in history if p.load_kw is not None and math.isfinite(p.load_kw) and p.load_kw >= 0]
    if not samples:
        return LoadModel("unknown", tz, note="no load readings in history")
    samples.sort(key=lambda p: p.timestamp)

    buckets: dict[tuple[object, int], list[float]] = defaultdict(list)
    for p in samples:
        local = p.timestamp.astimezone(tz)
        buckets[(local.date(), local.hour)].append(float(p.load_kw))  # type: ignore[arg-type]
    per_hour: dict[int, list[tuple[object, float]]] = defaultdict(list)
    for (day, hour), vals in sorted(buckets.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        per_hour[hour].append((day, sum(vals) / len(vals)))
    day_count = len({d for d, _ in buckets})

    if day_count >= MIN_PROFILE_DAYS and len(per_hour) >= MIN_PROFILE_HOURS:
        all_cells = {h: _cell([v for _, v in rows]) for h, rows in per_hour.items()}
        typed: dict[tuple[bool, int], _Cell] = {}
        for hour, rows in per_hour.items():
            by_type = {
                weekend: [v for d, v in rows if (d.weekday() in weekend_days) == weekend]  # type: ignore[attr-defined]
                for weekend in (False, True)
            }
            # Split only when BOTH day-types have enough days for this hour; a lopsided split would
            # compare a well-measured weekday with a one-off weekend.
            if all(len(vals) >= MIN_DAYS_PER_DAYTYPE_CELL for vals in by_type.values()):
                typed.update({(weekend, hour): _cell(vals) for weekend, vals in by_type.items()})
        return LoadModel(
            "profile",
            tz,
            points_used=len(samples),
            days=day_count,
            missing_hours=24 - len(per_hour),
            weekend_days=weekend_days,
            note=f"{len(samples)} readings over {day_count} day(s), {len(per_hour)}/24 hours covered",
            _all=all_cells,
            _typed=typed,
        )

    latest = samples[-1]
    if now - latest.timestamp <= FLAT_MAX_AGE:
        window = [
            float(p.load_kw)  # type: ignore[arg-type]
            for p in samples
            if latest.timestamp - p.timestamp <= timedelta(hours=1)
        ]
        flat = sum(window) / len(window)
        return LoadModel(
            "flat",
            tz,
            points_used=len(window),
            days=day_count,
            flat_kw=flat,
            weekend_days=weekend_days,
            note=f"history too thin for a profile ({day_count} day(s), {len(per_hour)}/24 hours); latest reading held flat",
        )
    return LoadModel(
        "unknown",
        tz,
        points_used=0,
        days=day_count,
        note="history too thin for a profile and the latest load reading is stale",
    )
