"""
Open-Meteo weather client.

Docs: https://open-meteo.com/en/docs
No API key. Attribution required (CC BY 4.0).

Hourly fields live in `config.OPEN_METEO_HOURLY_FIELDS` so new variables can
be requested later without rewriting the HTTP layer.

Weather is fetched once at startup and then every `weather_refresh_minutes`.
Each telemetry tick only looks up the cached hour (linear interpolation).
Radiation uses Open-Meteo's *_instant variables so interpolation between
hour stamps is not shifted by the preceding-hour averaging.

When live data is unavailable (or in fallback / historical-style mode) the
seeded synthetic climate in `engines/climate.py` is used instead.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from simulator.config import (
    OPEN_METEO_DAILY_FIELDS,
    OPEN_METEO_HOURLY_FIELDS,
    SimulatorConfig,
)
from simulator.engines.climate import climate_for
from simulator.engines.sun import (
    apparent_temperature_c,
    clear_sky,
    dew_point_c,
    humidity_from_dew_point,
    solar_position,
    sun_times,
    weather_condition,
)
from simulator.models import WEATHER_FIELD_MAP, WeatherRecord

logger = logging.getLogger("suryaa.weather")

_FAILED_FETCH_BACKOFF = timedelta(minutes=5)
_MAX_PAST_DAYS = 92
_MAX_FORECAST_DAYS = 16


def parse_local(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def approximate_sun_times(
    ts: datetime, latitude: float, longitude: float | None = None
) -> tuple[datetime, datetime]:
    """Sunrise/sunset when the API does not provide them.

    Without a longitude the zone's UTC offset is used as a proxy meridian.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if longitude is None:
        offset = ts.utcoffset() or timedelta(0)
        longitude = offset.total_seconds() / 3600.0 * 15.0
    return sun_times(ts, latitude, longitude)


def _climate(config: SimulatorConfig):
    return climate_for(
        round(config.latitude, 4),
        round(config.longitude, 4),
        float(config.location_elevation_m or 0.0),
        config.timezone,
        config.location_seed,
        config.clear_sky_turbidity,
    )


def fallback_weather(
    ts: datetime,
    config: SimulatorConfig,
    *,
    source: str = "fallback",
    data_quality: str = "fallback",
    cloud_cover: float | None = None,
    precipitation_mm: float | None = None,
) -> WeatherRecord:
    """Seeded synthetic climate for the configured location (no network)."""
    tz = ZoneInfo(config.timezone)
    local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
    values = _climate(config).sample(local)
    if cloud_cover is not None:
        values["cloud_cover_percent"] = cloud_cover
    if precipitation_mm is not None:
        values["precipitation_mm"] = precipitation_mm
    record = WeatherRecord(**values, source=source, data_quality=data_quality)
    return enrich_weather(record, config)


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def enrich_weather(record: WeatherRecord, config: SimulatorConfig) -> WeatherRecord:
    """Add sun geometry, clear-sky reference, condition text; round for readability."""
    data = record.model_copy(deep=True)
    local = data.timestamp
    sun = solar_position(local, config.latitude, config.longitude)
    sky = clear_sky(sun, float(config.location_elevation_m or 0.0), config.clear_sky_turbidity)

    if data.dew_point_c is None:
        data.dew_point_c = dew_point_c(data.temperature_c, data.humidity_percent)
    if data.apparent_temperature_c is None:
        data.apparent_temperature_c = apparent_temperature_c(
            data.temperature_c, data.humidity_percent, data.wind_speed_kmh
        )
    if data.wind_gusts_kmh is None:
        data.wind_gusts_kmh = data.wind_speed_kmh * 1.5
    if data.direct_normal_irradiance_wm2 is None:
        cos_z = sun.cos_zenith
        data.direct_normal_irradiance_wm2 = data.direct_radiation_wm2 / cos_z if cos_z > 0.05 else 0.0

    data.is_day = sun.is_up
    data.sun_elevation_deg = round(sun.elevation_deg, 2)
    data.sun_azimuth_deg = round(sun.azimuth_deg, 2)
    data.clear_sky_ghi_wm2 = round(sky.ghi, 1)
    data.clearness_index = (
        round(min(1.2, data.shortwave_radiation_wm2 / sky.ghi), 3) if sky.ghi > 50.0 else None
    )
    data.weather_condition = weather_condition(data.weather_code)

    data.temperature_c = round(data.temperature_c, 2)
    data.humidity_percent = round(data.humidity_percent, 1)
    data.cloud_cover_percent = round(data.cloud_cover_percent, 1)
    data.precipitation_mm = round(data.precipitation_mm, 2)
    data.precipitation_probability_percent = round(data.precipitation_probability_percent, 1)
    data.wind_speed_kmh = round(data.wind_speed_kmh, 1)
    data.shortwave_radiation_wm2 = round(data.shortwave_radiation_wm2, 1)
    data.direct_radiation_wm2 = round(data.direct_radiation_wm2, 1)
    data.diffuse_radiation_wm2 = round(data.diffuse_radiation_wm2, 1)
    data.direct_normal_irradiance_wm2 = _round(data.direct_normal_irradiance_wm2, 1)
    data.dew_point_c = _round(data.dew_point_c, 2)
    data.apparent_temperature_c = _round(data.apparent_temperature_c, 2)
    data.wind_gusts_kmh = _round(data.wind_gusts_kmh, 1)
    return data


def _scale_radiation(data: WeatherRecord, ghi_factor: float, beam_factor: float) -> None:
    data.shortwave_radiation_wm2 = data.shortwave_radiation_wm2 * ghi_factor
    data.direct_radiation_wm2 = min(data.shortwave_radiation_wm2, data.direct_radiation_wm2 * beam_factor)
    data.diffuse_radiation_wm2 = max(data.shortwave_radiation_wm2 - data.direct_radiation_wm2, 0.0)
    if data.direct_normal_irradiance_wm2 is not None:
        data.direct_normal_irradiance_wm2 = data.direct_normal_irradiance_wm2 * beam_factor


def _shift_temperature(data: WeatherRecord, delta_c: float, dew_delta_c: float = 0.0) -> None:
    dew = data.dew_point_c if data.dew_point_c is not None else dew_point_c(data.temperature_c, data.humidity_percent)
    data.temperature_c = data.temperature_c + delta_c
    data.dew_point_c = min(data.temperature_c - 0.5, dew + dew_delta_c)
    data.humidity_percent = humidity_from_dew_point(data.temperature_c, data.dew_point_c)
    data.apparent_temperature_c = None


def overlay_scenario(record: WeatherRecord, scenario: str) -> WeatherRecord:
    """Force weather traits for named scenarios without a second API call."""
    data = record.model_copy(deep=True)
    local = data.timestamp
    hours = local.hour + local.minute / 60.0
    if scenario == "cloudy_day":
        data.cloud_cover_percent = max(data.cloud_cover_percent, 88.0)
        _scale_radiation(data, 0.42, 0.25)
        data.weather_code = 3
        data.precipitation_probability_percent = max(data.precipitation_probability_percent, 40.0)
    elif scenario == "rainy_day":
        data.cloud_cover_percent = max(data.cloud_cover_percent, 92.0)
        data.precipitation_mm = max(data.precipitation_mm, 4.5)
        data.precipitation_probability_percent = 90.0
        _scale_radiation(data, 0.28, 0.12)
        data.weather_code = 63
        data.humidity_percent = min(100.0, max(data.humidity_percent, 88.0))
        data.dew_point_c = None
        data.apparent_temperature_c = None
    elif scenario == "monsoon_storm":
        if 15.0 <= hours < 17.5:
            data.cloud_cover_percent = 100.0
            data.precipitation_mm = max(data.precipitation_mm, 14.0)
            data.precipitation_probability_percent = 100.0
            _scale_radiation(data, 0.12, 0.02)
            data.wind_speed_kmh = max(data.wind_speed_kmh, 32.0)
            data.wind_gusts_kmh = max(data.wind_gusts_kmh or 0.0, 68.0)
            data.weather_code = 95
            _shift_temperature(data, -4.0, 1.0)
        elif 13.0 <= hours < 15.0:
            build = (hours - 13.0) / 2.0
            data.cloud_cover_percent = max(data.cloud_cover_percent, 60.0 + 35.0 * build)
            _scale_radiation(data, 1.0 - 0.6 * build, 1.0 - 0.85 * build)
            data.precipitation_probability_percent = max(data.precipitation_probability_percent, 80.0)
            data.weather_code = 3 if build > 0.5 else 2
        elif 17.5 <= hours < 20.0:
            data.cloud_cover_percent = max(data.cloud_cover_percent, 90.0)
            data.precipitation_mm = max(data.precipitation_mm, 2.0)
            _scale_radiation(data, 0.35, 0.1)
            data.weather_code = 61
            _shift_temperature(data, -3.0, 0.5)
    elif scenario == "heatwave":
        _shift_temperature(data, 5.5, -2.0)
        data.cloud_cover_percent = min(data.cloud_cover_percent, 20.0)
    elif scenario == "winter_cold":
        _shift_temperature(data, -7.0, -4.0)
    elif scenario == "sensor_failure":
        data.shortwave_radiation_wm2 = 0.0
        data.direct_radiation_wm2 = 0.0
        data.diffuse_radiation_wm2 = 0.0
        data.direct_normal_irradiance_wm2 = 0.0
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
        self._last_failure_wall: datetime | None = None
        self._last_success = False
        self._source = "fallback"
        self._quality = "fallback"

    async def warmup(self, ts: datetime) -> None:
        await self._maybe_refresh(ts, force=True)

    async def get_weather(self, ts: datetime, scenario: str | None = None) -> WeatherRecord:
        await self._maybe_refresh(ts, force=False)
        record = self._lookup(ts)
        overlaid = overlay_scenario(record, scenario or self.config.scenario)
        return enrich_weather(overlaid, self.config)

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
        recently_failed = (
            self._last_failure_wall is not None
            and now_wall - self._last_failure_wall < _FAILED_FETCH_BACKOFF
        )
        if not force and recently_failed:
            return

        try:
            await self._fetch_live(ts)
            self._last_success = True
            self._source = "open-meteo"
            self._quality = "simulated"
            self._last_fetch_wall = now_wall
            self._last_failure_wall = None
        except Exception as exc:  # noqa: BLE001 - keep generating
            logger.warning(
                "Open-Meteo request failed (%s); using cached/synthetic weather", exc
            )
            self._last_failure_wall = now_wall
            self._quality = "fallback"
            if self._hourly:
                self._source = "open-meteo-cached"
                self._last_success = True
            else:
                self._source = "fallback"
                self._last_success = False

    def _request_window(self, ts: datetime) -> tuple[int, int]:
        """past_days / forecast_days that cover `ts` (backfills and future runs)."""
        now = datetime.now(tz=self._tz)
        local = ts.astimezone(self._tz) if ts.tzinfo else ts.replace(tzinfo=self._tz)
        past = self.config.open_meteo_past_days
        forecast = self.config.open_meteo_forecast_days
        if local < now:
            past = max(past, math.ceil((now - local).total_seconds() / 86400.0) + 1)
        elif local > now:
            forecast = max(forecast, math.ceil((local - now).total_seconds() / 86400.0) + 2)
        return min(_MAX_PAST_DAYS, past), min(_MAX_FORECAST_DAYS, forecast)

    async def _fetch_live(self, ts: datetime | None = None) -> None:
        past_days, forecast_days = self._request_window(ts or datetime.now(tz=self._tz))
        params = {
            "latitude": self.config.latitude,
            "longitude": self.config.longitude,
            "timezone": self.config.timezone,
            "hourly": ",".join(OPEN_METEO_HOURLY_FIELDS),
            "daily": ",".join(OPEN_METEO_DAILY_FIELDS),
            "forecast_days": forecast_days,
            "past_days": past_days,
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
            "Fetched Open-Meteo hourly weather: %s hours (past_days=%s forecast_days=%s) for %s, %s",
            len(self._hourly),
            past_days,
            forecast_days,
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
            return sun_times(ts, self.config.latitude, self.config.longitude)
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
            return fallback_weather(local, self.config, source=source, data_quality=quality)

        hour0 = self._hour_key(local)
        hour1 = hour0 + timedelta(hours=1)
        row0 = self._hourly.get(hour0)
        row1 = self._hourly.get(hour1)
        if row0 is None:
            return fallback_weather(local, self.config, source="fallback", data_quality="fallback")

        frac = (local - hour0).total_seconds() / 3600.0
        mapped: dict[str, float | int | None] = {}
        for api_key, field_name in WEATHER_FIELD_MAP.items():
            v0 = row0.get(api_key)
            v1 = row1.get(api_key) if row1 else v0
            if api_key == "weather_code":
                mapped[field_name] = None if v0 is None else int(v0)
                continue
            if v0 is None and v1 is None:
                mapped[field_name] = None
            elif v0 is None:
                mapped[field_name] = float(v1 or 0.0)
            elif v1 is None:
                mapped[field_name] = float(v0)
            else:
                mapped[field_name] = float(v0) + (float(v1) - float(v0)) * frac

        def value(name: str) -> float:
            raw = mapped.get(name)
            return float(raw) if raw is not None else 0.0

        sunrise, sunset = self._sun_times(local)
        quality = self._quality if self._last_success else "fallback"
        return WeatherRecord(
            timestamp=local,
            temperature_c=value("temperature_c"),
            humidity_percent=value("humidity_percent"),
            cloud_cover_percent=value("cloud_cover_percent"),
            precipitation_mm=value("precipitation_mm"),
            precipitation_probability_percent=value("precipitation_probability_percent"),
            wind_speed_kmh=value("wind_speed_kmh"),
            shortwave_radiation_wm2=value("shortwave_radiation_wm2"),
            direct_radiation_wm2=value("direct_radiation_wm2"),
            diffuse_radiation_wm2=value("diffuse_radiation_wm2"),
            direct_normal_irradiance_wm2=mapped.get("direct_normal_irradiance_wm2"),
            dew_point_c=mapped.get("dew_point_c"),
            apparent_temperature_c=mapped.get("apparent_temperature_c"),
            wind_gusts_kmh=mapped.get("wind_gusts_kmh"),
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
