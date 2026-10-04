"""
Synthetic climate for `fallback` / `historical-style` weather (and live-mode
outages).

Instead of one identical day repeated forever, each simulated day gets:

- Seasonal normals. Indian subcontinent uses Pune-calibrated monthly normals
  adjusted for latitude (colder north-Indian winters, hotter pre-monsoon) and
  elevation. Elsewhere a hemisphere-aware generic profile is used.
- A weather regime (clear / partly_cloudy / overcast / rain / storm) from a
  persistent Markov chain, so clear spells and monsoon spells last several
  days like real weather.
- A day-to-day temperature anomaly, regime-dependent cloud build-up, rain
  spells, afternoon thunderstorms with gusts, and winter fog in north India.

Irradiance is physically consistent: clear-sky (sun.py) × cloud transmission
(Kasten–Czeplak), with beam/diffuse split derived from cloud cover.
All randomness is hashed from (location_seed, day, …) so it is reproducible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from simulator.engines import randomness as rnd
from simulator.engines.sun import (
    apparent_temperature_c,
    clear_sky,
    dew_point_c,
    humidity_from_dew_point,
    solar_position,
    sun_times,
)

REGIMES: tuple[str, ...] = ("clear", "partly_cloudy", "overcast", "rain", "storm")
_REGIME_PERSISTENCE = 0.58


@dataclass(frozen=True)
class MonthNormal:
    tmax: float
    tmin: float
    humidity: float
    weights: tuple[float, float, float, float, float]


# Pune / Pimpri-Chinchwad (18.5 °N, ~560 m) monthly normals + regime mix.
_INDIA_NORMALS: dict[int, MonthNormal] = {
    1: MonthNormal(30.0, 12.0, 45, (0.82, 0.15, 0.03, 0.00, 0.00)),
    2: MonthNormal(32.0, 13.0, 38, (0.80, 0.17, 0.03, 0.00, 0.00)),
    3: MonthNormal(35.5, 17.0, 32, (0.65, 0.28, 0.06, 0.01, 0.00)),
    4: MonthNormal(37.5, 21.0, 35, (0.55, 0.33, 0.08, 0.02, 0.02)),
    5: MonthNormal(36.5, 23.0, 48, (0.40, 0.38, 0.12, 0.05, 0.05)),
    6: MonthNormal(31.5, 23.0, 72, (0.08, 0.25, 0.27, 0.30, 0.10)),
    7: MonthNormal(28.0, 22.0, 84, (0.02, 0.13, 0.35, 0.42, 0.08)),
    8: MonthNormal(27.5, 21.5, 85, (0.03, 0.17, 0.35, 0.38, 0.07)),
    9: MonthNormal(29.5, 21.0, 80, (0.08, 0.27, 0.25, 0.32, 0.08)),
    10: MonthNormal(31.5, 18.5, 65, (0.40, 0.35, 0.12, 0.08, 0.05)),
    11: MonthNormal(30.5, 14.5, 52, (0.65, 0.27, 0.06, 0.02, 0.00)),
    12: MonthNormal(29.5, 12.0, 48, (0.78, 0.18, 0.04, 0.00, 0.00)),
}
_INDIA_REF_LAT = 18.5
_INDIA_REF_ELEV_M = 560.0


def _is_indian_subcontinent(latitude: float, longitude: float) -> bool:
    return 6.0 <= latitude <= 37.0 and 68.0 <= longitude <= 98.0


def _india_normal(month: int, latitude: float, elevation_m: float) -> MonthNormal:
    base = _INDIA_NORMALS[month]
    delta = latitude - _INDIA_REF_LAT
    tmax, tmin = base.tmax, base.tmin
    if month in (11, 12, 1, 2):
        tmax += {11: -0.25, 12: -0.75, 1: -0.9, 2: -0.75}[month] * delta
        tmin -= 0.70 * delta
    elif month in (4, 5, 6):
        tmax += 0.35 * delta
        tmin += 0.40 * delta
    else:
        tmax -= 0.10 * delta
        tmin -= 0.20 * delta
    lapse = -6.5 * (elevation_m - _INDIA_REF_ELEV_M) / 1000.0
    return MonthNormal(tmax + lapse, tmin + lapse, base.humidity, base.weights)


def _generic_normal(month: int, latitude: float, elevation_m: float) -> MonthNormal:
    mid_doy = (month - 1) * 30.4 + 15
    peak_doy = 200 if latitude >= 0 else 20
    seasonal = math.cos(2.0 * math.pi * (mid_doy - peak_doy) / 365.0)
    a = abs(latitude)
    annual_mean = 27.0 - 0.42 * max(0.0, a - 12.0)
    amplitude = min(16.0, 0.33 * max(0.0, a - 10.0))
    tmean = annual_mean + amplitude * seasonal - 6.5 * max(0.0, elevation_m) / 1000.0
    if seasonal > 0.3:
        weights = (0.45, 0.30, 0.12, 0.10, 0.03)
    elif seasonal < -0.3:
        weights = (0.25, 0.30, 0.25, 0.18, 0.02)
    else:
        weights = (0.35, 0.30, 0.18, 0.15, 0.02)
    return MonthNormal(tmean + 5.5, tmean - 5.5, 65.0, weights)


@lru_cache(maxsize=4096)
def month_normal(month: int, latitude: float, longitude: float, elevation_m: float) -> MonthNormal:
    if _is_indian_subcontinent(latitude, longitude):
        return _india_normal(month, latitude, elevation_m)
    return _generic_normal(month, latitude, elevation_m)


def _blend(a: MonthNormal, b: MonthNormal, w: float) -> MonthNormal:
    def mix(x: float, y: float) -> float:
        return x + (y - x) * w

    return MonthNormal(
        mix(a.tmax, b.tmax),
        mix(a.tmin, b.tmin),
        mix(a.humidity, b.humidity),
        tuple(mix(x, y) for x, y in zip(a.weights, b.weights)),  # type: ignore[arg-type]
    )


@dataclass
class DayWeather:
    day: date
    regime: str
    tmax: float
    tmin: float
    dew_c: float
    cloud_base: float
    wind_base: float
    rain_hours: dict[int, float] = field(default_factory=dict)
    storm_start: float | None = None
    storm_end: float | None = None
    storm_mm_per_h: float = 0.0
    fog_until: float | None = None
    monsoon: bool = False


class SyntheticClimate:
    def __init__(
        self,
        *,
        latitude: float,
        longitude: float,
        elevation_m: float,
        timezone: str,
        seed: int,
        turbidity: float = 0.92,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.elevation_m = elevation_m
        self.tz = ZoneInfo(timezone)
        self.seed = seed
        self.turbidity = turbidity
        self._regimes: dict[date, str] = {}
        self._days: dict[date, DayWeather] = {}

    def normal_for(self, day: date) -> MonthNormal:
        here = month_normal(day.month, self.latitude, self.longitude, self.elevation_m)
        # Blend toward the neighbouring month so seasons change smoothly.
        if day.day >= 15:
            nxt = day.month % 12 + 1
            other = month_normal(nxt, self.latitude, self.longitude, self.elevation_m)
            w = (day.day - 15) / 30.0
        else:
            prv = (day.month - 2) % 12 + 1
            other = month_normal(prv, self.latitude, self.longitude, self.elevation_m)
            w = (15 - day.day) / 30.0
        return _blend(here, other, max(0.0, min(0.5, w)))

    def _weights(self, day: date) -> dict[str, float]:
        normal = self.normal_for(day)
        return dict(zip(REGIMES, normal.weights))

    def regime(self, day: date) -> str:
        cached = self._regimes.get(day)
        if cached is not None:
            return cached
        # Walk the chain from a fixed anchor (12 warm-up days before each
        # 30-day block). The walk is deterministic, so every day in a block
        # gets the same answer no matter which day a run started on.
        block_start = (day.toordinal() // 30) * 30
        current = date.fromordinal(block_start - 12)
        state = rnd.weighted_choice(self._weights(current), self.seed, "regime0", current)
        while current < day:
            current = current + timedelta(days=1)
            weights = self._weights(current)
            keep = weights.get(state, 0.0) > 0.01 and rnd.chance(_REGIME_PERSISTENCE, self.seed, "persist", current)
            if not keep:
                state = rnd.weighted_choice(weights, self.seed, "regime", current)
            if current.toordinal() >= block_start:
                self._regimes[current] = state
        return state

    def day(self, day: date) -> DayWeather:
        cached = self._days.get(day)
        if cached is not None:
            return cached
        normal = self.normal_for(day)
        regime = self.regime(day)
        seed = self.seed
        monsoon = normal.weights[3] + normal.weights[4] >= 0.3

        anomaly = 1.6 * rnd.normal(seed, "tanom", day)
        effects = {
            "clear": (0.8, -0.8, -1.0),
            "partly_cloudy": (0.0, 0.0, 0.0),
            "overcast": (-2.5, 0.8, 1.5),
            "rain": (-4.5, 0.5, 2.5),
            "storm": (-1.0, 0.5, 2.0),
        }
        dmax, dmin, ddew = effects[regime]
        # Normals already include rainy days; remove the month's average regime
        # effect so the monthly mean stays on the normal.
        total_w = sum(normal.weights) or 1.0
        mean_dmax = sum(w * effects[r][0] for r, w in zip(REGIMES, normal.weights)) / total_w
        mean_dmin = sum(w * effects[r][1] for r, w in zip(REGIMES, normal.weights)) / total_w
        tmax = normal.tmax + anomaly + dmax - mean_dmax
        tmin = normal.tmin + 0.7 * anomaly + dmin - mean_dmin
        if tmax - tmin < 3.0:
            tmin = tmax - 3.0
        tmean = (tmax + tmin) / 2.0
        dew = min(tmean - 1.0, dew_point_c(tmean, normal.humidity) + ddew)

        cloud_ranges = {
            "clear": (3.0, 15.0),
            "partly_cloudy": (25.0, 55.0),
            "overcast": (75.0, 92.0),
            "rain": (85.0, 98.0),
            "storm": (25.0, 50.0),
        }
        low, high = cloud_ranges[regime]
        if monsoon and regime == "partly_cloudy":
            # A "break" day in the monsoon is still mostly covered.
            low, high = low + 15.0, high + 15.0
        cloud_base = rnd.uniform_between(low, high, seed, "cloud", day)
        wind_base = rnd.uniform_between(12.0, 22.0, seed, "wind", day) if monsoon else rnd.uniform_between(5.0, 11.0, seed, "wind", day)

        weather = DayWeather(
            day=day,
            regime=regime,
            tmax=tmax,
            tmin=tmin,
            dew_c=dew,
            cloud_base=cloud_base,
            wind_base=wind_base,
            monsoon=monsoon,
        )

        if regime == "rain":
            hours = 3 + int(rnd.uniform(seed, "rainhours", day) * 8)
            for i in range(hours):
                # Afternoon / evening bias, monsoon nights also rain.
                hour = int(rnd.weighted_choice(
                    {str(h): (1.6 if 13 <= h <= 21 else 1.0) for h in range(24)},
                    seed, "rainhour", day, i,
                ))
                intensity = math.exp(0.8 * rnd.normal(seed, "rainmm", day, i))
                weather.rain_hours[hour] = round(min(25.0, intensity), 2)
        elif regime == "partly_cloudy" and monsoon and rnd.chance(0.25, seed, "shower", day):
            hour = 14 + int(rnd.uniform(seed, "showerhour", day) * 6)
            weather.rain_hours[hour] = round(rnd.uniform_between(0.3, 2.0, seed, "showermm", day), 2)
        elif regime == "storm":
            start = rnd.uniform_between(14.5, 18.5, seed, "stormstart", day)
            weather.storm_start = start
            weather.storm_end = start + rnd.uniform_between(1.0, 2.5, seed, "stormlen", day)
            weather.storm_mm_per_h = rnd.uniform_between(8.0, 25.0, seed, "stormmm", day)

        north_winter = self.latitude > 23.0 and day.month in (12, 1, 2)
        if north_winter and regime in ("clear", "partly_cloudy") and rnd.chance(0.35, seed, "fog", day):
            weather.fog_until = rnd.uniform_between(8.5, 11.0, seed, "fogend", day)

        self._days[day] = weather
        return weather

    def _temperature(self, local: datetime, hours: float, sunrise_h: float, solar_noon_h: float) -> float:
        today = self.day(local.date())
        yesterday = self.day(local.date() - timedelta(days=1))
        tomorrow = self.day(local.date() + timedelta(days=1))
        peak = solar_noon_h + 2.3
        if hours < sunrise_h:
            span = sunrise_h + 24.0 - peak
            x = (hours + 24.0 - peak) / span
            return today.tmin + (yesterday.tmax - today.tmin) * 0.5 * (1.0 + math.cos(math.pi * x))
        if hours < peak:
            x = (hours - sunrise_h) / max(0.5, peak - sunrise_h)
            return today.tmin + (today.tmax - today.tmin) * math.sin(math.pi / 2.0 * x)
        span = sunrise_h + 24.0 - peak
        x = (hours - peak) / span
        return tomorrow.tmin + (today.tmax - tomorrow.tmin) * 0.5 * (1.0 + math.cos(math.pi * x))

    def sample(self, ts: datetime) -> dict[str, float | int | str | datetime | None]:
        local = ts.astimezone(self.tz) if ts.tzinfo else ts.replace(tzinfo=self.tz)
        today = self.day(local.date())
        seed = self.seed
        hours = local.hour + local.minute / 60.0 + local.second / 3600.0
        sunrise, sunset = sun_times(local, self.latitude, self.longitude)
        sunrise_h = sunrise.hour + sunrise.minute / 60.0
        sunset_h = sunset.hour + sunset.minute / 60.0
        solar_noon_h = (sunrise_h + sunset_h) / 2.0

        temperature = self._temperature(local, hours, sunrise_h, solar_noon_h)

        t_seconds = local.timestamp()
        wobble = (rnd.smooth_noise(t_seconds, 7200.0, seed, "cloudwobble") - 0.5) * 20.0
        cloud = today.cloud_base + wobble
        if today.regime == "partly_cloudy":
            cloud += 18.0 * max(0.0, math.sin(math.pi * (hours - 11.0) / 7.0))
        elif today.regime == "clear":
            cloud = min(cloud, 20.0)

        precip = today.rain_hours.get(local.hour, 0.0)
        storm_active = False
        if today.storm_start is not None and today.storm_end is not None:
            if hours >= today.storm_start - 2.0:
                build = min(1.0, (hours - (today.storm_start - 2.0)) / 2.0)
                cloud = cloud + (97.0 - cloud) * build
            if today.storm_start <= hours < today.storm_end:
                storm_active = True
                precip = max(precip, today.storm_mm_per_h)
            elif today.storm_end <= hours < today.storm_end + 2.0:
                precip = max(precip, 1.2)
        if precip > 0.0:
            cloud = max(cloud, 85.0)
            temperature -= min(4.0, precip * 0.6)
        cloud = min(100.0, max(0.0, cloud))

        fog = today.fog_until is not None and hours < today.fog_until and hours >= sunrise_h - 3.0
        dew = today.dew_c + (1.5 if precip > 0 else 0.0)
        dew = min(dew, temperature - (0.2 if fog or precip > 0 else 1.0))
        humidity = humidity_from_dew_point(temperature, dew)
        if fog:
            humidity = max(humidity, 95.0)

        diurnal_wind = 0.7 + 0.6 * max(0.0, math.sin(math.pi * (hours - 9.0) / 12.0))
        wind = today.wind_base * diurnal_wind * (0.85 + 0.3 * rnd.smooth_noise(t_seconds, 1800.0, seed, "wind"))
        gusts = wind * 1.55
        if storm_active:
            wind += 25.0
            gusts = wind + rnd.uniform_between(25.0, 45.0, seed, "gust", today.day)
        if fog:
            wind = min(wind, 4.0)
            gusts = wind * 1.5

        sun = solar_position(local, self.latitude, self.longitude)
        sky = clear_sky(sun, self.elevation_m, self.turbidity)
        c = cloud / 100.0
        transmission = 1.0 - 0.75 * c**3.4
        beam_fraction = max(0.0, 1.0 - c) ** 1.4
        if today.regime in ("overcast", "rain"):
            # Monsoon nimbostratus is far thicker than a winter overcast deck.
            transmission *= 0.62 if today.monsoon else 0.8
        if precip > 0.0:
            transmission *= max(0.3, 1.0 - 0.08 * precip)
            beam_fraction *= 0.2
        if fog:
            transmission *= 0.45
            beam_fraction *= 0.15
        ghi = sky.ghi * transmission
        dni = sky.dni * beam_fraction
        beam_h = dni * sun.cos_zenith
        if beam_h > 0.9 * ghi:
            beam_h = 0.9 * ghi
            dni = beam_h / max(sun.cos_zenith, 1e-3)
        dhi = max(0.0, ghi - beam_h)

        if today.regime == "clear":
            precip_prob = 2.0
        elif today.regime == "partly_cloudy":
            precip_prob = 30.0 if today.monsoon else 10.0
        elif today.regime == "overcast":
            precip_prob = 35.0
        elif today.regime == "rain":
            precip_prob = 80.0
        else:
            precip_prob = 95.0 if storm_active else 60.0
        if precip > 0:
            precip_prob = max(precip_prob, 85.0)

        if storm_active:
            code = 95
        elif fog:
            code = 45
        elif precip >= 7.6:
            code = 65
        elif precip >= 2.5:
            code = 63
        elif precip > 0.1:
            code = 80 if today.regime == "partly_cloudy" else 61
        elif cloud >= 85:
            code = 3
        elif cloud >= 50:
            code = 2
        elif cloud >= 20:
            code = 1
        else:
            code = 0

        return {
            "timestamp": local,
            "temperature_c": temperature,
            "humidity_percent": humidity,
            "dew_point_c": dew,
            "apparent_temperature_c": apparent_temperature_c(temperature, humidity, wind),
            "cloud_cover_percent": cloud,
            "precipitation_mm": precip,
            "precipitation_probability_percent": precip_prob,
            "wind_speed_kmh": wind,
            "wind_gusts_kmh": gusts,
            "shortwave_radiation_wm2": ghi,
            "direct_radiation_wm2": beam_h,
            "diffuse_radiation_wm2": dhi,
            "direct_normal_irradiance_wm2": dni,
            "weather_code": code,
            "sunrise": sunrise,
            "sunset": sunset,
            "day_regime": today.regime,
        }


@lru_cache(maxsize=64)
def climate_for(
    latitude: float,
    longitude: float,
    elevation_m: float,
    timezone: str,
    seed: int,
    turbidity: float,
) -> SyntheticClimate:
    return SyntheticClimate(
        latitude=latitude,
        longitude=longitude,
        elevation_m=elevation_m,
        timezone=timezone,
        seed=seed,
        turbidity=turbidity,
    )
