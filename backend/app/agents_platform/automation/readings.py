"""
The one input the rule engine consumes: a `Reading`.

WHY a separate dataclass instead of `TelemetryRecord`: the engine must run in a worker, in a dry run over history and
in tests without importing the DB or ingest models, and it must be immune to ingest-schema churn. `reading_from_record`
is the single place that knows the telemetry field names.

Unknown is a first-class value. Every numeric field may be None ("sensor offline", "feed missing"); non-finite floats
are coerced to None at construction, because NaN compares False with everything and would make `not (x < 5)` true.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any

# Same statuses `domain/alerts.py` treats as an outage; kept in step by a parity test.
GRID_DOWN_STATUSES = frozenset({"outage", "off", "disconnected", "islanded"})
# Data qualities whose numbers cannot be trusted at all. "degraded" keeps its values (alerts.py only warns about it).
UNUSABLE_DATA_QUALITIES = frozenset({"missing", "disconnected"})


def ensure_utc(value: datetime) -> datetime:
    """Naive datetimes are taken as UTC, matching `energy.schemas._aware`."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class Reading:
    """One telemetry tick, reduced to what rules can talk about. Powers are kW, percentages 0-100."""

    timestamp: datetime
    soc_percent: float | None = None
    battery_temperature_c: float | None = None
    solar_kw: float | None = None
    load_kw: float | None = None
    grid_import_kw: float | None = None
    grid_export_kw: float | None = None
    grid_available: bool | None = None
    battery_discharge_kw: float | None = None
    battery_charge_kw: float | None = None
    device_power_kw: Mapping[str, float | None] = field(default_factory=dict)
    price_import_inr_per_kwh: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_utc(self.timestamp))
        for f in fields(self):
            if f.name in {"timestamp", "grid_available", "device_power_kw"}:
                continue
            object.__setattr__(self, f.name, _finite(getattr(self, f.name)))
        if self.grid_available is not None:
            object.__setattr__(self, "grid_available", bool(self.grid_available))
        object.__setattr__(
            self,
            "device_power_kw",
            {str(key): _finite(value) for key, value in dict(self.device_power_kw).items()},
        )


def _get(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def reading_from_record(record: Any, *, price_inr_per_kwh: float | None = None) -> Reading:
    """
    Adapt a `TelemetryRecord` (or an equivalent dict / DB row) to a `Reading`.

    - Device keys are the telemetry `device_id`s; the validation allow-list must use the same keys.
    - `grid_status` in alerts.py's outage set -> `grid_available=False`; an absent status -> unknown (None).
    - `data_quality` of missing/disconnected blanks every telemetry metric: the numbers are placeholders.
    - Battery temperature is not a core telemetry field; it is read from `battery_temperature_c` if the producer sends it.
    """
    timestamp = _get(record, "timestamp")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if not isinstance(timestamp, datetime):
        raise ValueError("record has no usable timestamp")

    quality = str(_get(record, "data_quality", "") or "").lower()
    if quality in UNUSABLE_DATA_QUALITIES:
        return Reading(timestamp=timestamp, price_import_inr_per_kwh=price_inr_per_kwh)

    status = _get(record, "grid_status")
    grid_available: bool | None = None
    if status:
        grid_available = str(status).lower() not in GRID_DOWN_STATUSES

    device_power: dict[str, float | None] = {}
    for device in _get(record, "devices", None) or []:
        key = _get(device, "device_id")
        if key:
            device_power[str(key)] = _finite(_get(device, "current_power_kw"))

    return Reading(
        timestamp=timestamp,
        soc_percent=_get(record, "battery_soc_percent"),
        battery_temperature_c=_get(record, "battery_temperature_c"),
        solar_kw=_get(record, "solar_power_kw"),
        load_kw=_get(record, "home_load_power_kw"),
        grid_import_kw=_get(record, "grid_import_power_kw"),
        grid_export_kw=_get(record, "grid_export_power_kw"),
        grid_available=grid_available,
        battery_discharge_kw=_get(record, "battery_discharge_power_kw"),
        battery_charge_kw=_get(record, "battery_charge_power_kw"),
        device_power_kw=device_power,
        price_import_inr_per_kwh=price_inr_per_kwh,
    )


def make_reading(timestamp: datetime, **values: Any) -> Reading:
    """Test/dry-run helper: `make_reading(ts, soc_percent=15, grid_available=True)`. Unlisted fields are unknown."""
    return Reading(timestamp=timestamp, **values)


def make_series(
    start: datetime,
    step_seconds: float,
    **columns: Sequence[Any],
) -> list[Reading]:
    """
    Build an evenly spaced series from per-field value lists, e.g.
    `make_series(t0, 300, soc_percent=[50, 15, 15], grid_available=[True] * 3)`.
    Shorter columns are padded with None, so a column can end early to model a feed dropping out.
    """
    length = max((len(values) for values in columns.values()), default=0)
    series: list[Reading] = []
    for index in range(length):
        row = {name: (values[index] if index < len(values) else None) for name, values in columns.items()}
        series.append(Reading(timestamp=start + timedelta(seconds=step_seconds * index), **row))
    return series
