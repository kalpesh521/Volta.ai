"""
ForecastResult: the engine's output. Frozen pydantic models, so `model_dump(mode="json")` is
JSON-safe and `to_facts()` can hand an LLM prompt a compact, pre-computed fact dict.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SolarMethod = Literal["physics+calibrated", "physics_default"]
LoadMethodLabel = Literal["profile", "flat", "unknown"]
ConfidenceLevel = Literal["high", "medium", "low"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ForecastSlot(_Frozen):
    """One hour. `start` is local time; powers are the mean kW over [start, start + 1 h)."""

    start: datetime
    lead_hours: float
    cloud_cover_pct: float
    precipitation_mm: float | None = None
    temperature_c: float | None = None
    solar_kw_p10: float
    solar_kw_p50: float
    solar_kw_p90: float
    load_kw_p10: float | None = None
    load_kw_p50: float | None = None
    load_kw_p90: float | None = None

    @property
    def net_kw_p50(self) -> float | None:
        return None if self.load_kw_p50 is None else self.solar_kw_p50 - self.load_kw_p50


class ForecastTotals(_Frozen):
    solar_kwh: float
    # Sum of per-hour p10 / p90: assumes the cloud error is fully correlated across hours, which is
    # the conservative (wider) reading. They bound the day; they are not a probabilistic p10/p90.
    solar_kwh_p10: float
    solar_kwh_p90: float
    load_kwh: float | None = None
    net_kwh: float | None = None


class ForecastMethods(_Frozen):
    solar: SolarMethod
    load: LoadMethodLabel
    weather: str
    weather_kind: Literal["model", "persistence", "climatology"]


class Confidence(_Frozen):
    level: ConfidenceLevel
    reasons: list[str]


class DataPointsUsed(_Frozen):
    solar_calibration: int
    load_profile: int
    history_total: int


class ForecastResult(_Frozen):
    generated_at: datetime
    timezone: str
    horizon_hours: int
    slots: list[ForecastSlot]
    totals: ForecastTotals
    methods: ForecastMethods
    confidence: Confidence
    data_points_used: DataPointsUsed
    performance_ratio: float = Field(description="effective ratio used (default x calibration factor)")
    calibration_factor: float
    interval_basis: dict[str, str]
    warnings: list[str] = Field(default_factory=list)

    def to_facts(self, *, include_hourly: bool = True) -> dict[str, Any]:
        """Compact, JSON-friendly facts for the answer prompt (see facts.py)."""
        from .facts import build_facts

        return build_facts(self, include_hourly=include_hourly)
