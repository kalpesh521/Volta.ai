"""
Virtual household devices.

Definitions live in `config.DEFAULT_DEVICE_CATALOG` (or DEVICE_CATALOG_JSON).
To add a device, append a catalog dict. To remove one, delete it from the catalog.

Schedule modes: cyclic, windows, temperature_or_windows, always_on.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.models import DeviceReading, WeatherRecord

_IMPORTANT_POWER_KW = 1.5


def _device_priority(critical: bool, rated_power_kw: float) -> str:
    if critical:
        return "critical"
    if rated_power_kw >= _IMPORTANT_POWER_KW:
        return "important"
    return "flexible"


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _in_window(local: datetime, start: str, end: str) -> bool:
    current = local.timetz().replace(tzinfo=None)
    start_t = _parse_hhmm(start)
    end_t = _parse_hhmm(end)
    if start_t <= end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t


class DeviceEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.catalog: list[dict[str, Any]] = config.device_catalog()

    def step(self, ts: datetime, weather: WeatherRecord) -> list[DeviceReading]:
        tz = ZoneInfo(self.config.timezone)
        local = ts.astimezone(tz)
        readings: list[DeviceReading] = []
        for spec in self.catalog:
            on, power = self._is_on(spec, local, weather)
            interval_kwh = power * self.config.interval_hours
            device_id = spec["device_id"]
            rated = float(spec["rated_power_kw"])
            critical = bool(spec.get("critical", False))
            readings.append(
                DeviceReading(
                    device_id=device_id,
                    device_name=spec["device_name"],
                    device_type=spec["device_type"],
                    rated_power_kw=rated,
                    current_state="on" if on else "off",
                    current_power_kw=round(power, 4),
                    energy_interval_kwh=round(interval_kwh, 6),
                    critical=critical,
                    controllable=bool(spec.get("controllable", True)),
                    device_priority=_device_priority(critical, rated),
                )
            )
        return readings

    def _is_on(
        self,
        spec: dict[str, Any],
        local: datetime,
        weather: WeatherRecord,
    ) -> tuple[bool, float]:
        rated = float(spec["rated_power_kw"])
        schedule = spec.get("schedule") or {"mode": "always_on"}
        mode = schedule.get("mode", "always_on")

        if (
            self.config.scenario == "high_evening_load"
            and spec["device_type"] == "air_conditioner"
            and 17 <= local.hour <= 23
        ):
            return True, rated

        on = False
        if mode == "always_on":
            on = True
        elif mode == "cyclic":
            on_minutes = int(schedule.get("on_minutes", 25))
            cycle_minutes = int(schedule.get("cycle_minutes", 45))
            minute_of_day = local.hour * 60 + local.minute
            on = (minute_of_day % cycle_minutes) < on_minutes
        elif mode == "windows":
            on = any(
                _in_window(local, window["start"], window["end"])
                for window in schedule.get("windows", [])
            )
        elif mode == "temperature_or_windows":
            window_on = any(
                _in_window(local, window["start"], window["end"])
                for window in schedule.get("windows", [])
            )
            temp_on = weather.temperature_c >= float(schedule.get("on_above_c", 29.0))
            on = window_on or temp_on

        if on and spec["device_type"] == "washing_machine":
            if (local.toordinal() + self.config.random_seed) % 2 == 1:
                on = False

        power = rated if on else 0.0
        return on, power
