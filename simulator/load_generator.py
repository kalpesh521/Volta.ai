"""
Household electrical load: base demand + occupancy + appliances + noise.

Daily shape:
    00:00–05:00  low
    06:00–09:00  morning peak
    10:00–16:00  moderate
    17:00–23:00  evening peak
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.device_engine import DeviceEngine
from simulator.models import DeviceReading, WeatherRecord


def _band(hour: float) -> str:
    if 0 <= hour < 6:
        return "night"
    if 6 <= hour < 10:
        return "morning"
    if 10 <= hour < 17:
        return "midday"
    return "evening"


BASE_LOAD_KW = {
    "night": 0.22,
    "morning": 0.42,
    "midday": 0.36,
    "evening": 0.58,
}

OCCUPANCY = {
    "night": 0.25,
    "morning": 1.00,
    "midday": 0.45,
    "evening": 1.00,
}


class LoadGenerator:
    def __init__(
        self, config: SimulatorConfig, device_engine: DeviceEngine | None = None
    ) -> None:
        self.config = config
        self.device_engine = device_engine or DeviceEngine(config)
        self.consumption_today_kwh = 0.0
        self.consumption_total_kwh = 0.0
        self._today = None

    def reset_daily_if_needed(self, ts: datetime) -> None:
        tz = ZoneInfo(self.config.timezone)
        local_date = ts.astimezone(tz).date()
        if self._today != local_date:
            self._today = local_date
            self.consumption_today_kwh = 0.0

    def base_load_kw(self, ts: datetime) -> float:
        tz = ZoneInfo(self.config.timezone)
        local = ts.astimezone(tz)
        hour = local.hour + local.minute / 60.0
        band = _band(hour)
        base = BASE_LOAD_KW[band]
        occupancy = OCCUPANCY[band]
        rng = random.Random(self.config.random_seed + int(ts.timestamp()))
        noise = rng.uniform(0.92, 1.08)
        load = base * (0.55 + 0.45 * occupancy) * noise
        if self.config.scenario == "high_evening_load" and band == "evening":
            load *= 1.85
        return load

    def generate(
        self, ts: datetime, weather: WeatherRecord
    ) -> tuple[float, list[DeviceReading]]:
        devices = self.device_engine.step(ts, weather)
        device_power = sum(item.current_power_kw for item in devices)
        demand_kw = self.base_load_kw(ts) + device_power
        return round(max(0.0, demand_kw), 4), devices

    def record_served(self, served_kw: float, ts: datetime) -> dict[str, float]:
        self.reset_daily_if_needed(ts)
        interval_kwh = served_kw * self.config.interval_hours
        self.consumption_today_kwh += interval_kwh
        self.consumption_total_kwh += interval_kwh
        return {
            "home_consumption_interval_kwh": round(interval_kwh, 6),
            "home_consumption_today_kwh": round(self.consumption_today_kwh, 6),
            "home_consumption_total_kwh": round(self.consumption_total_kwh, 6),
        }
