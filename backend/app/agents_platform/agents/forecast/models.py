"""
Inputs and ports of the forecast engine.

Plain dataclasses on purpose: the engine must not depend on the DB models or on the telemetry
pydantic schema. The agent layer maps `TelemetryRecord` -> `HistoryPoint` (a four-line adapter), and
the engine stays a pure function that is trivial to test and to run offline.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .solar import DEFAULT_PERFORMANCE_RATIO

DEFAULT_HORIZON_HOURS = 24
MAX_HORIZON_HOURS = 48


class ForecastWeatherError(Exception):
    """A weather source could not provide the requested hours (network, HTTP, parse, coverage).

    Typed so the engine can degrade to persistence instead of failing the whole forecast."""


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One telemetry sample. Either value may be absent (sensor drop-out). Naive timestamps are UTC."""

    timestamp: datetime
    solar_kw: float | None = None
    load_kw: float | None = None


@dataclass(frozen=True, slots=True)
class WeatherHour:
    """Weather at one instant. Only cloud cover is required; the rest improves the physics."""

    time: datetime
    cloud_cover_pct: float
    precipitation_mm: float | None = None
    temperature_c: float | None = None


WeatherKind = Literal["model", "persistence", "climatology"]


@dataclass(frozen=True, slots=True)
class WeatherSeries:
    """What a weather port returns: one entry per requested time, in the same order.

    `kind` tells the engine how much to trust it: a numerical model is trusted more than
    persistence, which is trusted more than a bare climatological mean."""

    source: str
    kind: WeatherKind
    hours: tuple[WeatherHour, ...]


@runtime_checkable
class WeatherForecastPort(Protocol):
    async def hourly(
        self, *, latitude: float, longitude: float, times: Sequence[datetime]
    ) -> WeatherSeries:
        """Weather at each of `times` (tz-aware). Raise ForecastWeatherError if it cannot."""
        ...


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    latitude: float
    longitude: float
    timezone: str
    solar_capacity_kwp: float
    inverter_capacity_kw: float
    history: Sequence[HistoryPoint] = field(default_factory=tuple)
    horizon_hours: int = DEFAULT_HORIZON_HOURS
    tilt_deg: float | None = None
    azimuth_deg: float | None = None
    performance_ratio: float = DEFAULT_PERFORMANCE_RATIO
    # Latest observed cloud cover, if the caller has one; seeds the persistence fallback.
    observed_cloud_cover_pct: float | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not (-90.0 <= self.latitude <= 90.0) or not (-180.0 <= self.longitude <= 180.0):
            raise ValueError("latitude/longitude out of range")
        for name in ("solar_capacity_kwp", "inverter_capacity_kw", "performance_ratio"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
