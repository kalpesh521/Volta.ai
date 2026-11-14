"""
The CLOSED metric registry: the only things a rule may talk about.

WHY closed: rules are written by an LLM from natural language. If the vocabulary is open, a misspelt or invented metric
silently never fires (or worse, matches the wrong thing). Each metric declares its unit, the plausible range that a
threshold must fall inside (so `battery.soc_percent > 150` is rejected instead of being a rule that never fires) and how
to read it from a `Reading`. Device power metrics are generated from an allow-list at validation time, never free-form.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from app.agents_platform.automation.readings import Reading

ReadFn = Callable[[Reading, datetime], float | None]

DEVICE_METRIC_RE = re.compile(r"^device\.([A-Za-z0-9_-]{1,48})\.power_kw$")


@dataclass(frozen=True)
class MetricSpec:
    id: str
    unit: str
    min_value: float
    max_value: float
    description: str
    read: ReadFn
    discrete: bool = False
    # Smallest hysteresis band in this metric's unit; 0 means hysteresis is meaningless for it (booleans, clock).
    min_band: float = 0.0
    # Capability the home must have for the metric to exist: "battery" or "price" (None = always available).
    needs: str | None = None

    @property
    def supports_hysteresis(self) -> bool:
        return self.min_band > 0

    def in_range(self, value: float) -> bool:
        return self.min_value <= value <= self.max_value


def _attr(name: str) -> ReadFn:
    return lambda reading, _local_now: getattr(reading, name)


def _grid_available(reading: Reading, _local_now: datetime) -> float | None:
    if reading.grid_available is None:
        return None
    return 1.0 if reading.grid_available else 0.0


def _hour_local(_reading: Reading, local_now: datetime) -> float | None:
    return float(local_now.hour)


_spec = MetricSpec  # positional shorthand for the table below


_STATIC: tuple[MetricSpec, ...] = (
    _spec("battery.soc_percent", "%", 0, 100, "Battery state of charge", _attr("soc_percent"),
          min_band=1.0, needs="battery"),
    _spec("battery.temperature_c", "degC", -20, 80, "Battery temperature", _attr("battery_temperature_c"),
          min_band=1.0, needs="battery"),
    _spec("battery.discharge_kw", "kW", 0, 50, "Power flowing out of the battery", _attr("battery_discharge_kw"),
          min_band=0.05, needs="battery"),
    _spec("battery.charge_kw", "kW", 0, 50, "Power flowing into the battery", _attr("battery_charge_kw"),
          min_band=0.05, needs="battery"),
    _spec("solar.power_kw", "kW", 0, 50, "Solar generation", _attr("solar_kw"), min_band=0.05),
    _spec("load.power_kw", "kW", 0, 50, "Whole-home consumption", _attr("load_kw"), min_band=0.05),
    _spec("grid.import_kw", "kW", 0, 50, "Power drawn from the grid", _attr("grid_import_kw"), min_band=0.05),
    _spec("grid.export_kw", "kW", 0, 50, "Power sent to the grid", _attr("grid_export_kw"), min_band=0.05),
    _spec("grid.available", "bool", 0, 1, "1 when the grid supply is available, 0 during an outage",
          _grid_available, discrete=True),
    _spec("time.hour_local", "hour", 0, 23, "Hour of day (0-23) in the home's timezone", _hour_local),
    _spec("price.import_inr_per_kwh", "INR/kWh", 0, 100, "Current grid import tariff", _attr("price_import_inr_per_kwh"),
          min_band=0.25, needs="price"),
)

METRICS: dict[str, MetricSpec] = {spec.id: spec for spec in _STATIC}


@lru_cache(maxsize=256)
def _device_spec(key: str) -> MetricSpec:
    def read(reading: Reading, _local_now: datetime) -> float | None:
        return reading.device_power_kw.get(key)

    return MetricSpec(
        id=f"device.{key}.power_kw",
        unit="kW",
        min_value=0.0,
        max_value=20.0,
        description=f"Power drawn by device '{key}'",
        read=read,
        min_band=0.05,
    )


def device_key_of(metric_id: str) -> str | None:
    match = DEVICE_METRIC_RE.match(metric_id)
    return match.group(1) if match else None


def resolve_metric(metric_id: str, device_keys: Collection[str] | None = None) -> MetricSpec | None:
    """
    The spec for `metric_id`, or None if it is not in the closed vocabulary.
    `device_keys=None` accepts any well-formed device metric (shape only; used while parsing and evaluating);
    a collection restricts devices to that allow-list (used by validation).
    """
    static = METRICS.get(metric_id)
    if static is not None:
        return static
    key = device_key_of(metric_id)
    if key is None or (device_keys is not None and key not in device_keys):
        return None
    return _device_spec(key)


def read_metric(metric_id: str, reading: Reading, local_now: datetime) -> float | None:
    """Value of a metric for this reading, None if unknown. An unresolvable id reads as unknown, never raises."""
    spec = resolve_metric(metric_id)
    return None if spec is None else spec.read(reading, local_now)


def metric_catalog() -> list[dict[str, object]]:
    """Prompt-ready description of the vocabulary for the NL-to-DSL agent."""
    catalog: list[dict[str, object]] = [
        {
            "id": spec.id,
            "unit": spec.unit,
            "min": spec.min_value,
            "max": spec.max_value,
            "description": spec.description,
            "needs": spec.needs,
        }
        for spec in METRICS.values()
    ]
    catalog.append(
        {
            "id": "device.<device_key>.power_kw",
            "unit": "kW",
            "min": 0.0,
            "max": 20.0,
            "description": "Power drawn by one allowed device",
            "needs": None,
        }
    )
    return catalog
