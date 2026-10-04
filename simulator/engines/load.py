"""
Household electrical load = untracked loads + tracked devices.

Occupancy follows a smooth daily curve (weekday vs Sunday/Saturday, wake-time
jitter, optional work-from-home). Untracked loads every Indian home has are
modelled explicitly and reported as `load_breakdown_kw`:

    standby        router, set-top box, chargers, appliance electronics
    lighting       LEDs when it is dark (sunset, heavy cloud) and people are awake
    fans           ceiling fans driven by apparent temperature, also at night
    kitchen        meal-time spikes (mixer, induction, kettle, microwave)
    entertainment  TV in the evening, laptops/phones during the day
    misc           ironing / vacuum bursts on some days

Tracked devices (fridge, AC, geyser, …) come from `DeviceEngine`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.engines import randomness as rnd
from simulator.engines.device import DeviceEngine
from simulator.models import DeviceReading, WeatherRecord

# Activity level 0–1 by hour (fraction of residents at home and awake).
_WEEKDAY = (
    0.08, 0.06, 0.06, 0.06, 0.08, 0.30, 0.80, 1.00, 0.90, 0.50, 0.40, 0.40,
    0.50, 0.55, 0.45, 0.45, 0.50, 0.65, 0.85, 1.00, 1.00, 0.95, 0.70, 0.35,
)
_WEEKEND = (
    0.10, 0.08, 0.06, 0.06, 0.06, 0.08, 0.15, 0.40, 0.80, 1.00, 0.90, 0.85,
    0.90, 0.95, 0.70, 0.60, 0.65, 0.60, 0.60, 0.85, 1.00, 1.00, 0.85, 0.50,
)
# People at home (awake or asleep) at night use fans/AC.
_SLEEP_PRESENCE = 0.95

# Kept for consumers that imported these names.
BASE_LOAD_KW = {"night": 0.22, "morning": 0.42, "midday": 0.36, "evening": 0.58}
OCCUPANCY = {"night": 0.25, "morning": 1.00, "midday": 0.45, "evening": 1.00}


def _band(hour: float) -> str:
    if 0 <= hour < 6:
        return "night"
    if 6 <= hour < 10:
        return "morning"
    if 10 <= hour < 17:
        return "midday"
    return "evening"


def _interp(curve: tuple[float, ...], hours: float) -> float:
    hours %= 24.0
    i = int(hours)
    frac = hours - i
    return curve[i] + (curve[(i + 1) % 24] - curve[i]) * frac


class LoadGenerator:
    def __init__(
        self, config: SimulatorConfig, device_engine: DeviceEngine | None = None
    ) -> None:
        self.config = config
        self.device_engine = device_engine or DeviceEngine(config)
        self.consumption_today_kwh = 0.0
        self.consumption_total_kwh = 0.0
        self.peak_today_kw = 0.0
        self._today: date | None = None
        self.last_breakdown: dict[str, float] = {}
        self.last_occupancy = 0.0

    def reset_daily_if_needed(self, ts: datetime) -> None:
        tz = ZoneInfo(self.config.timezone)
        local_date = ts.astimezone(tz).date()
        if self._today != local_date:
            self._today = local_date
            self.consumption_today_kwh = 0.0
            self.peak_today_kw = 0.0

    def _local(self, ts: datetime) -> datetime:
        return ts.astimezone(ZoneInfo(self.config.timezone))

    def occupancy(self, ts: datetime, scenario: str | None = None) -> float:
        cfg = self.config
        scenario = scenario or cfg.scenario
        local = self._local(ts)
        day = local.date()
        if scenario == "vacation":
            return 0.0
        # Wake/sleep times drift ±25 min day to day.
        shift = rnd.uniform_between(-0.42, 0.42, cfg.household_seed, "wake-shift", day)
        hours = local.hour + local.minute / 60.0 + local.second / 3600.0 - shift
        weekday = local.weekday()
        if weekday == 6:
            level = _interp(_WEEKEND, hours)
        elif weekday == 5:
            level = 0.5 * (_interp(_WEEKDAY, hours) + _interp(_WEEKEND, hours))
        else:
            level = _interp(_WEEKDAY, hours)
            if cfg.work_from_home and 9.0 <= hours < 18.0:
                level = min(1.0, level + 0.35)
        if scenario == "guests_party" and 17.0 <= hours < 24.0:
            level = min(1.6, level * 1.6)
        return level

    def _presence(self, local: datetime, occupancy: float, scenario: str) -> float:
        if scenario == "vacation":
            return 0.0
        hour = local.hour
        if hour >= 23 or hour < 6:
            return max(occupancy, _SLEEP_PRESENCE)
        return occupancy

    def breakdown(
        self,
        ts: datetime,
        weather: WeatherRecord | None,
        scenario: str | None = None,
        devices: list[DeviceReading] | None = None,
    ) -> dict[str, float]:
        cfg = self.config
        scenario = scenario or cfg.scenario
        local = self._local(ts)
        day = local.date()
        seed = cfg.household_seed
        occupants = max(1, cfg.household_occupants)
        occupancy = self.occupancy(ts, scenario)
        presence = self._presence(local, occupancy, scenario)
        minute = local.hour * 60.0 + local.minute + local.second / 60.0
        weekend = local.weekday() >= 5
        t_seconds = local.timestamp()

        standby = 0.045 + 0.008 * occupants
        if scenario == "vacation":
            standby = 0.04

        elevation = weather.sun_elevation_deg if weather and weather.sun_elevation_deg is not None else (
            30.0 if 7 <= local.hour < 18 else -10.0
        )
        darkness = min(1.0, max(0.0, (10.0 - elevation) / 12.0))
        if weather is not None and weather.shortwave_radiation_wm2 < 120.0 and elevation > 0:
            darkness = max(darkness, 0.6)
        lighting = darkness * min(1.0, occupancy) * 0.04 * (1.0 + occupants / 2.0)
        if presence > occupancy:
            lighting += 0.01 * darkness

        feels = 28.0
        if weather is not None:
            feels = weather.apparent_temperature_c if weather.apparent_temperature_c is not None else weather.temperature_c
        indoor = 26.0 + 0.6 * (feels - 26.0)
        fan_fraction = min(1.0, max(0.0, (indoor - 24.0) / 7.0))
        fans_count = min(occupants, 5)
        ac_running = any(
            d.device_type == "air_conditioner" and d.current_state == "on" for d in (devices or [])
        )
        fans = fans_count * 0.07 * fan_fraction * min(1.0, presence)
        if ac_running:
            fans *= 0.5

        kitchen = 0.0
        if scenario != "vacation":
            meals = (
                ("breakfast", 7 * 60 + 15, 30.0, 20.0, 0.5, 1.0),
                ("lunch", 12 * 60 + 30, 30.0, 25.0, 0.55, 1.0 if weekend else 0.6),
                ("tea", 16 * 60 + 45, 20.0, 10.0, 0.4, 0.7),
                ("dinner", 19 * 60 + 45, 40.0, 35.0, 0.7, 1.0),
            )
            party = 1.6 if scenario == "guests_party" else 1.0
            for name, center, jitter, duration, level, probability in meals:
                if not rnd.chance(probability, seed, "meal", name, day):
                    continue
                start = center + rnd.uniform_between(-jitter, jitter, seed, "mealstart", name, day)
                length = duration * (1.3 if name == "dinner" and party > 1 else 1.0)
                if start <= minute < start + length:
                    burst = rnd.uniform(seed, "kitchen", int(t_seconds // 60))
                    kitchen = level * party * (0.3 + 1.4 * burst)
                    break

        entertainment = 0.0
        if scenario != "vacation":
            evening_tv = 19 * 60 <= minute < 23 * 60
            weekend_tv = weekend and 11 * 60 <= minute < 14 * 60
            if (evening_tv or weekend_tv) and occupancy > 0.4:
                entertainment += 0.09
            entertainment += 0.03 * min(1.0, occupancy) * occupants / 4.0
            if cfg.work_from_home and local.weekday() < 5 and 9 * 60 <= minute < 18 * 60:
                entertainment += 0.12

        misc = 0.0
        if scenario != "vacation" and rnd.chance(0.4, seed, "iron", day):
            start = rnd.uniform_between(8 * 60, 9.5 * 60, seed, "ironstart", day)
            if start <= minute < start + rnd.uniform_between(10.0, 20.0, seed, "irondur", day):
                misc = 1.0 * (0.55 + 0.45 * rnd.uniform(seed, "ironcycle", int(t_seconds // 45)))

        breakdown = {
            "standby": standby,
            "lighting": lighting,
            "fans": fans,
            "kitchen": kitchen,
            "entertainment": entertainment,
            "misc": misc,
        }
        jitter = 1.0 + 0.06 * (rnd.smooth_noise(t_seconds, 300.0, seed, "load-jitter") - 0.5)
        if scenario == "high_evening_load" and _band(local.hour + local.minute / 60.0) == "evening":
            jitter *= 1.85
        return {key: round(value * jitter, 4) for key, value in breakdown.items()}

    def base_load_kw(
        self,
        ts: datetime,
        weather: WeatherRecord | None = None,
        scenario: str | None = None,
        devices: list[DeviceReading] | None = None,
    ) -> float:
        return sum(self.breakdown(ts, weather, scenario, devices).values())

    def generate(
        self, ts: datetime, weather: WeatherRecord, scenario: str | None = None
    ) -> tuple[float, list[DeviceReading]]:
        scenario = scenario or self.config.scenario
        occupancy = self.occupancy(ts, scenario)
        devices = self.device_engine.step(ts, weather, scenario, occupancy)
        breakdown = self.breakdown(ts, weather, scenario, devices)
        device_power = sum(item.current_power_kw for item in devices)
        breakdown["tracked_devices"] = round(device_power, 4)
        self.last_breakdown = breakdown
        self.last_occupancy = round(occupancy, 3)
        demand_kw = sum(value for key, value in breakdown.items() if key != "tracked_devices") + device_power
        return round(max(0.0, demand_kw), 4), devices

    def record_served(self, served_kw: float, ts: datetime, demand_kw: float | None = None) -> dict[str, float]:
        self.reset_daily_if_needed(ts)
        interval_kwh = served_kw * self.config.interval_hours
        self.consumption_today_kwh += interval_kwh
        self.consumption_total_kwh += interval_kwh
        self.peak_today_kw = max(self.peak_today_kw, demand_kw if demand_kw is not None else served_kw)
        return {
            "home_consumption_interval_kwh": round(interval_kwh, 6),
            "home_consumption_today_kwh": round(self.consumption_today_kwh, 6),
            "home_consumption_total_kwh": round(self.consumption_total_kwh, 6),
        }

    def state(self) -> dict[str, Any]:
        return {
            "consumption_today_kwh": self.consumption_today_kwh,
            "consumption_total_kwh": self.consumption_total_kwh,
            "peak_today_kw": self.peak_today_kw,
            "today": self._today.isoformat() if self._today else None,
            "devices": self.device_engine.state(),
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.consumption_today_kwh = float(data.get("consumption_today_kwh", 0.0))
        self.consumption_total_kwh = float(data.get("consumption_total_kwh", 0.0))
        self.peak_today_kw = float(data.get("peak_today_kw", 0.0))
        self._today = date.fromisoformat(data["today"]) if data.get("today") else None
        self.device_engine.restore(data.get("devices") or {})
