"""
Solar PV production from weather.

    solar_power_kw =
        solar_capacity_kwp
        * irradiance_factor
        * solar_efficiency
        * shading_factor
        * temperature_factor
        * cloud_factor      # only when GHI is synthesized
        * rain_factor       # only when GHI is synthesized
        * noise

Irradiance comes from Open-Meteo shortwave radiation (GHI, W/m² / 1000).
Measured GHI already includes clouds and rain — do not derate again.
If GHI is missing, a daylight sine curve is used and then cloud/rain apply.
"""

from __future__ import annotations

import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.models import WeatherRecord
from simulator.clients.weather import approximate_sun_times


def _seeded_rng(seed: int, ts: datetime) -> random.Random:
    return random.Random(seed + int(ts.timestamp()))


def _sun_window(
    ts: datetime, weather: WeatherRecord, latitude: float
) -> tuple[datetime, datetime]:
    sunrise = weather.sunrise
    sunset = weather.sunset
    if sunrise is None or sunset is None:
        return approximate_sun_times(ts, latitude)
    return sunrise, sunset


def _is_night(ts: datetime, weather: WeatherRecord, latitude: float) -> bool:
    sunrise, sunset = _sun_window(ts, weather, latitude)
    return ts < sunrise or ts > sunset


def daylight_irradiance_wm2(
    ts: datetime, weather: WeatherRecord, latitude: float
) -> float:
    sunrise, sunset = _sun_window(ts, weather, latitude)
    if ts < sunrise or ts > sunset:
        return 0.0
    day_len = (sunset - sunrise).total_seconds()
    if day_len <= 0:
        return 0.0
    elapsed = (ts - sunrise).total_seconds()
    return 1000.0 * max(0.0, math.sin(math.pi * elapsed / day_len))


def temperature_factor(temperature_c: float, coefficient: float) -> float:
    """STC is 25 °C. Typical c-Si coefficient is about -0.4 % / °C."""
    factor = 1.0 - coefficient * (temperature_c - 25.0)
    return min(1.08, max(0.70, factor))


def cloud_factor(cloud_cover_percent: float) -> float:
    return 1.0 - 0.40 * (cloud_cover_percent / 100.0)


def rain_factor(precipitation_mm: float) -> float:
    return 1.0 - min(0.40, max(0.0, precipitation_mm) * 0.10)


class SolarGenerator:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.energy_today_kwh = 0.0
        self.energy_total_kwh = 0.0
        self._today = None

    def reset_daily_if_needed(self, ts: datetime) -> None:
        tz = ZoneInfo(self.config.timezone)
        local_date = ts.astimezone(tz).date()
        if self._today != local_date:
            self._today = local_date
            self.energy_today_kwh = 0.0

    def compute_power_kw(self, ts: datetime, weather: WeatherRecord) -> float:
        if _is_night(ts, weather, self.config.latitude):
            return 0.0
        if weather.data_quality == "degraded" and weather.shortwave_radiation_wm2 <= 0.0:
            return 0.0

        ghi = weather.shortwave_radiation_wm2
        measured_ghi = ghi > 0.0
        if not measured_ghi:
            ghi = daylight_irradiance_wm2(ts, weather, self.config.latitude)

        irradiance_factor = min(1.2, max(0.0, ghi / 1000.0))
        temp_f = temperature_factor(
            weather.temperature_c, self.config.pv_temp_coefficient_per_c
        )
        # Open-Meteo / fallback GHI already includes clouds and rain.
        # Extra derate only when we synthesize a clear-sky sine curve.
        cloud_f = 1.0 if measured_ghi else cloud_factor(weather.cloud_cover_percent)
        rain_f = 1.0 if measured_ghi else rain_factor(weather.precipitation_mm)
        rng = _seeded_rng(self.config.random_seed, ts)
        noise = rng.uniform(0.97, 1.03)

        power = (
            self.config.solar_capacity_kwp
            * irradiance_factor
            * self.config.solar_efficiency
            * self.config.shading_factor
            * temp_f
            * cloud_f
            * rain_f
            * noise
        )
        return min(max(0.0, power), self.config.inverter_capacity_kw)

    def accumulate(self, ts: datetime, power_kw: float) -> dict[str, float]:
        """Record delivered (post-dispatch) solar energy for this interval."""
        self.reset_daily_if_needed(ts)
        power_kw = max(0.0, power_kw)
        interval_kwh = power_kw * self.config.interval_hours
        self.energy_today_kwh += interval_kwh
        self.energy_total_kwh += interval_kwh
        return {
            "solar_power_kw": round(power_kw, 4),
            "solar_energy_interval_kwh": round(interval_kwh, 6),
            "solar_energy_today_kwh": round(self.energy_today_kwh, 6),
            "solar_energy_total_kwh": round(self.energy_total_kwh, 6),
        }
