"""
Open-Meteo weather client.

Docs: https://open-meteo.com/en/docs
No API key. Attribution required (CC BY 4.0).

Hourly fields live in `config.OPEN_METEO_HOURLY_FIELDS` so new variables can
be requested later without rewriting the HTTP layer.

Weather is fetched once at startup and then every `weather_refresh_minutes`.
Each telemetry tick only looks up the cached hour (linear interpolation).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from simulator.config import (
    OPEN_METEO_DAILY_FIELDS,
    OPEN_METEO_HOURLY_FIELDS,
    SimulatorConfig,
)
from simulator.models import WEATHER_FIELD_MAP, WeatherRecord

logger = logging.getLogger("suryaa.weather")


def parse_local(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def approximate_sun_times(ts: datetime, latitude: float) -> tuple[datetime, datetime]:
    """Simple seasonal sunrise/sunset when the API does not provide them."""
    day_of_year = ts.timetuple().tm_yday
    decl = 23.44 * math.sin(math.radians(360.0 / 365.0 * (day_of_year - 81)))
    lat_rad = math.radians(latitude)
    decl_rad = math.radians(decl)
    arg = -math.tan(lat_rad) * math.tan(decl_rad)
    arg = min(1.0, max(-1.0, arg))
    hour_angle = math.degrees(math.acos(arg))
    daylight_hours = 2.0 * hour_angle / 15.0
    noon = ts.replace(hour=12, minute=0, second=0, microsecond=0)
    half = timedelta(hours=daylight_hours / 2.0)
    return noon - half, noon + half


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def fallback_weather(
    ts: datetime,
    config: SimulatorConfig,
    *,
    source: str = "fallback",
    data_quality: str = "fallback",
    cloud_cover: float | None = None,
    precipitation_mm: float | None = None,
) -> WeatherRecord:
    """Deterministic Pune / Pimpri-Chinchwad profile when live data is unavailable."""
    tz = ZoneInfo(config.timezone)
    local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
    sunrise, sunset = approximate_sun_times(local, config.latitude)
    hour = local.hour + local.minute / 60.0
    day_frac = (local.timetuple().tm_yday - 1) / 365.0

    monsoon = 0.5 + 0.5 * math.sin(2.0 * math.pi * (day_frac - 0.45))
    temperature = 24.0 + 7.0 * math.sin((hour - 9.0) / 24.0 * 2.0 * math.pi)
    humidity = _clamp(55.0 + 25.0 * monsoon - 0.6 * (temperature - 24.0), 30.0, 98.0)

    if cloud_cover is None:
        cloud_cover = _clamp(
            25.0 + 50.0 * monsoon + 8.0 * math.sin(hour / 24.0 * 2.0 * math.pi),
            5.0,
            98.0,
        )
    if precipitation_mm is None:
        rain_peak = max(0.0, math.sin((hour - 16.0) / 24.0 * 2.0 * math.pi))
        precipitation_mm = round(
            max(0.0, (cloud_cover - 70.0) / 30.0 * 2.5 * monsoon * rain_peak),
            2,
        )

    if local < sunrise or local > sunset:
        ghi = 0.0
    else:
        day_len = (sunset - sunrise).total_seconds()
        elapsed = (local - sunrise).total_seconds()
        sine = math.sin(math.pi * elapsed / day_len) if day_len > 0 else 0.0
        clear_sky = 1050.0 * max(0.0, sine)
        ghi = clear_sky * (1.0 - 0.65 * (cloud_cover / 100.0))
        ghi *= max(0.35, 1.0 - 0.12 * precipitation_mm)

    diffuse = ghi * (0.25 + 0.55 * (cloud_cover / 100.0))
    direct = max(0.0, ghi - diffuse)
    wind = 8.0 + 4.0 * math.sin((hour - 14.0) / 24.0 * 2.0 * math.pi)
    precip_prob = _clamp(cloud_cover * 0.7 + precipitation_mm * 8.0, 0.0, 100.0)

    weather_code = 0
    if precipitation_mm >= 2.0:
        weather_code = 63
    elif precipitation_mm > 0.1:
        weather_code = 61
    elif cloud_cover >= 80:
        weather_code = 3
    elif cloud_cover >= 40:
        weather_code = 2

    return WeatherRecord(
        timestamp=local,
        temperature_c=round(temperature, 2),
        humidity_percent=round(humidity, 1),
        cloud_cover_percent=round(cloud_cover, 1),
        precipitation_mm=round(precipitation_mm, 2),
        precipitation_probability_percent=round(precip_prob, 1),
        wind_speed_kmh=round(max(0.0, wind), 1),
        shortwave_radiation_wm2=round(max(0.0, ghi), 1),
        direct_radiation_wm2=round(max(0.0, direct), 1),
        diffuse_radiation_wm2=round(max(0.0, diffuse), 1),
        weather_code=weather_code,
        sunrise=sunrise,
        sunset=sunset,
        source=source,
        data_quality=data_quality,
    )


def overlay_scenario(record: WeatherRecord, scenario: str) -> WeatherRecord:
    """Force weather traits for named scenarios without a second API call."""
    data = record.model_copy(deep=True)
    if scenario == "cloudy_day":
        data.cloud_cover_percent = max(data.cloud_cover_percent, 88.0)
        data.shortwave_radiation_wm2 = round(data.shortwave_radiation_wm2 * 0.42, 1)
        data.direct_radiation_wm2 = round(data.direct_radiation_wm2 * 0.25, 1)
        data.diffuse_radiation_wm2 = round(
            max(data.shortwave_radiation_wm2 - data.direct_radiation_wm2, 0.0), 1
        )
        data.weather_code = 3
        data.precipitation_probability_percent = max(
            data.precipitation_probability_percent, 40.0
        )
    elif scenario == "rainy_day":
        data.cloud_cover_percent = max(data.cloud_cover_percent, 92.0)
        data.precipitation_mm = max(data.precipitation_mm, 4.5)
        data.precipitation_probability_percent = 90.0
        data.shortwave_radiation_wm2 = round(data.shortwave_radiation_wm2 * 0.28, 1)
        data.direct_radiation_wm2 = round(data.direct_radiation_wm2 * 0.12, 1)
        data.diffuse_radiation_wm2 = round(
            max(data.shortwave_radiation_wm2 - data.direct_radiation_wm2, 0.0), 1
        )
        data.weather_code = 63
        data.humidity_percent = min(100.0, max(data.humidity_percent, 88.0))
    elif scenario == "sensor_failure":
        data.shortwave_radiation_wm2 = 0.0
        data.direct_radiation_wm2 = 0.0
        data.diffuse_radiation_wm2 = 0.0
        data.weather_code = None
        data.data_quality = "degraded"
        data.source = f"{data.source}+sensor_failure"
    return data


class WeatherClient:
    """Caches Open-Meteo hourly rows and serves interpolated WeatherRecords."""

    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self._tz = ZoneInfo(config.timezone)
        self._hourly: dict[datetime, dict[str, float | int | None]] = {}
        self._sunrise_by_day: dict[date, datetime] = {}
        self._sunset_by_day: dict[date, datetime] = {}
        self._last_fetch_wall: datetime | None = None
        self._last_success = False
        self._source = "fallback"
        self._quality = "fallback"

    async def warmup(self, ts: datetime) -> None:
        await self._maybe_refresh(ts, force=True)

    async def get_weather(self, ts: datetime) -> WeatherRecord:
        await self._maybe_refresh(ts, force=False)
        record = self._lookup(ts)
        return overlay_scenario(record, self.config.scenario)

    async def _maybe_refresh(self, ts: datetime, force: bool) -> None:
        mode = self.config.weather_mode
        if mode in ("fallback", "historical-style"):
            self._source = "historical-style" if mode == "historical-style" else "fallback"
            self._quality = "fallback" if mode == "fallback" else "simulated"
            self._last_success = True
            return

        now_wall = datetime.now(tz=self._tz)
        stale = self._last_fetch_wall is None or (
            now_wall - self._last_fetch_wall
            >= timedelta(minutes=self.config.weather_refresh_minutes)
        )
        missing = self._hour_key(ts) not in self._hourly
        if not force and not stale and not missing and self._hourly:
            return

        try:
            await self._fetch_live()
            self._last_success = True
            self._source = "open-meteo"
            self._quality = "simulated"
            self._last_fetch_wall = now_wall
        except Exception as exc:  # noqa: BLE001 - keep generating
            logger.warning(
                "Open-Meteo request failed (%s); using cached/fallback weather", exc
            )
            self._quality = "fallback"
            if self._hourly:
                self._source = "open-meteo-cached"
                self._last_success = True
            else:
                self._source = "fallback"
                self._last_success = False

    async def _fetch_live(self) -> None:
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "timezone": self.config.timezone,
            "hourly": ",".join(OPEN_METEO_HOURLY_FIELDS),
            "daily": ",".join(OPEN_METEO_DAILY_FIELDS),
            "forecast_days": self.config.open_meteo_forecast_days,
            "past_days": self.config.open_meteo_past_days,
            "wind_speed_unit": "kmh",
        }
        timeout = httpx.Timeout(self.config.open_meteo_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(self.config.open_meteo_base_url, params=params)
            response.raise_for_status()
            payload = response.json()

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        self._hourly.clear()
        for index, raw_time in enumerate(times):
            hour_ts = parse_local(raw_time, self._tz)
            if hour_ts is None:
                continue
            row: dict[str, float | int | None] = {}
            for api_key in OPEN_METEO_HOURLY_FIELDS:
                values = hourly.get(api_key) or []
                row[api_key] = values[index] if index < len(values) else None
            self._hourly[self._hour_key(hour_ts)] = row

        daily = payload.get("daily") or {}
        daily_times = daily.get("time") or []
        sunrises = daily.get("sunrise") or []
        sunsets = daily.get("sunset") or []
        self._sunrise_by_day.clear()
        self._sunset_by_day.clear()
        for index, raw_day in enumerate(daily_times):
            day = date.fromisoformat(str(raw_day)[:10])
            if index < len(sunrises):
                parsed = parse_local(sunrises[index], self._tz)
                if parsed:
                    self._sunrise_by_day[day] = parsed
            if index < len(sunsets):
                parsed = parse_local(sunsets[index], self._tz)
                if parsed:
                    self._sunset_by_day[day] = parsed

        logger.info(
            "Fetched Open-Meteo hourly weather: %s hours for %s, %s",
            len(self._hourly),
            self.config.latitude,
            self.config.longitude,
        )

    def _hour_key(self, ts: datetime) -> datetime:
        local = ts.astimezone(self._tz) if ts.tzinfo else ts.replace(tzinfo=self._tz)
        return local.replace(minute=0, second=0, microsecond=0)

    def _sun_times(self, ts: datetime) -> tuple[datetime, datetime]:
        day = ts.date()
        sunrise = self._sunrise_by_day.get(day)
        sunset = self._sunset_by_day.get(day)
        if sunrise is None or sunset is None:
            return approximate_sun_times(ts, self.config.latitude)
        return sunrise, sunset

    def _lookup(self, ts: datetime) -> WeatherRecord:
        local = ts.astimezone(self._tz) if ts.tzinfo else ts.replace(tzinfo=self._tz)
        mode = self.config.weather_mode

        if mode in ("fallback", "historical-style") or not self._hourly:
            source = self._source
            quality = self._quality
            if mode == "historical-style":
                source = "historical-style"
                quality = "simulated"
            if not self._hourly and mode == "live":
                source = "fallback"
                quality = "fallback"
            return fallback_weather(
                local, self.config, source=source, data_quality=quality
            )

        hour0 = self._hour_key(local)
        hour1 = hour0 + timedelta(hours=1)
        row0 = self._hourly.get(hour0)
        row1 = self._hourly.get(hour1)
        if row0 is None:
            return fallback_weather(
                local, self.config, source="fallback", data_quality="fallback"
            )

        frac = (local - hour0).total_seconds() / 3600.0
        mapped: dict[str, float | int | None] = {}
        for api_key, field_name in WEATHER_FIELD_MAP.items():
            v0 = row0.get(api_key)
            v1 = row1.get(api_key) if row1 else v0
            if api_key == "weather_code":
                mapped[field_name] = None if v0 is None else int(v0)
                continue
            if v0 is None and v1 is None:
                mapped[field_name] = 0.0
            elif v0 is None:
                mapped[field_name] = float(v1 or 0.0)
            elif v1 is None:
                mapped[field_name] = float(v0)
            else:
                mapped[field_name] = float(v0) + (float(v1) - float(v0)) * frac

        sunrise, sunset = self._sun_times(local)
        quality = self._quality if self._last_success else "fallback"
        return WeatherRecord(
            timestamp=local,
            temperature_c=float(mapped.get("temperature_c") or 0.0),
            humidity_percent=float(mapped.get("humidity_percent") or 0.0),
            cloud_cover_percent=float(mapped.get("cloud_cover_percent") or 0.0),
            precipitation_mm=float(mapped.get("precipitation_mm") or 0.0),
            precipitation_probability_percent=float(
                mapped.get("precipitation_probability_percent") or 0.0
            ),
            wind_speed_kmh=float(mapped.get("wind_speed_kmh") or 0.0),
            shortwave_radiation_wm2=float(mapped.get("shortwave_radiation_wm2") or 0.0),
            direct_radiation_wm2=float(mapped.get("direct_radiation_wm2") or 0.0),
            diffuse_radiation_wm2=float(mapped.get("diffuse_radiation_wm2") or 0.0),
            weather_code=(
                int(mapped["weather_code"])
                if mapped.get("weather_code") is not None
                else None
            ),
            sunrise=sunrise,
            sunset=sunset,
            source=self._source,
            data_quality=quality,
        )
