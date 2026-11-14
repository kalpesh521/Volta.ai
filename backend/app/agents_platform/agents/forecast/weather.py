"""
Weather sources for the forecast engine.

`PersistenceWeather`  no network. Holds the last observed cloud cover and relaxes toward a
                      climatological mean as the horizon grows (cloud is persistent for hours, not days).
`OpenMeteoWeather`    free Open-Meteo hourly forecast, async httpx, TTL cache, typed failure.

Open-Meteo returns values on UTC hour marks. Local hours in e.g. Asia/Kolkata sit on :30 UTC, so
requested instants are linearly interpolated between the two surrounding marks.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

import httpx

from .models import ForecastWeatherError, WeatherHour, WeatherSeries

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CLIMATOLOGICAL_CLOUD_PCT = 40.0
DEFAULT_PERSISTENCE_TAU_HOURS = 12.0


def _utc(ts: datetime) -> datetime:
    return (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(timezone.utc)


class PersistenceWeather:
    """cloud(t) = climatology + (observed - climatology) * exp(-lead / tau), lead measured from
    `observed_at` (default: the first requested time). Without an observation it is just the
    climatological mean, labelled as such so confidence can reflect it."""

    def __init__(
        self,
        *,
        observed_cloud_pct: float | None = None,
        observed_at: datetime | None = None,
        climatological_cloud_pct: float = DEFAULT_CLIMATOLOGICAL_CLOUD_PCT,
        tau_hours: float = DEFAULT_PERSISTENCE_TAU_HOURS,
    ) -> None:
        if tau_hours <= 0:
            raise ValueError("tau_hours must be positive")
        self._observed = observed_cloud_pct
        self._observed_at = observed_at
        self._clim = climatological_cloud_pct
        self._tau = tau_hours

    async def hourly(
        self, *, latitude: float, longitude: float, times: Sequence[datetime]
    ) -> WeatherSeries:
        if not times:
            return WeatherSeries("persistence", "persistence", ())
        has_observation = self._observed is not None and math.isfinite(self._observed)
        origin = _utc(self._observed_at) if self._observed_at is not None else _utc(times[0])
        hours: list[WeatherHour] = []
        for t in times:
            if has_observation:
                lead_h = max(0.0, (_utc(t) - origin).total_seconds() / 3600.0)
                cloud = self._clim + (float(self._observed) - self._clim) * math.exp(-lead_h / self._tau)
            else:
                cloud = self._clim
            hours.append(WeatherHour(time=t, cloud_cover_pct=max(0.0, min(100.0, cloud))))
        if has_observation:
            return WeatherSeries("persistence", "persistence", tuple(hours))
        return WeatherSeries("climatology", "climatology", tuple(hours))


class OpenMeteoWeather:
    """Open-Meteo hourly forecast (cloud_cover, temperature_2m, precipitation).

    Cache: keyed by lat/lon rounded to 2 decimals (~1 km, well inside a weather-model grid cell), so
    neighbouring homes share one upstream call. TTL default 30 min; Open-Meteo refreshes about
    hourly. Not single-flight: two concurrent cold requests for one key both fetch (harmless).
    """

    name = "open-meteo"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
        ttl_seconds: float = 1800.0,
        base_url: str = OPEN_METEO_URL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._url = base_url
        self._clock = clock
        self._cache: dict[tuple[float, float], tuple[float, dict[datetime, WeatherHour]]] = {}

    async def hourly(
        self, *, latitude: float, longitude: float, times: Sequence[datetime]
    ) -> WeatherSeries:
        key = (round(latitude, 2), round(longitude, 2))
        marks = self._fresh(key)
        if marks is None or not self._covers(marks, times):
            marks = await self._fetch(key)
            self._cache[key] = (self._clock(), marks)
        return WeatherSeries("open-meteo", "model", tuple(self._at(marks, t) for t in times))

    def _fresh(self, key: tuple[float, float]) -> dict[datetime, WeatherHour] | None:
        entry = self._cache.get(key)
        if entry is None or self._clock() - entry[0] >= self._ttl:
            return None
        return entry[1]

    @staticmethod
    def _bracket(marks: dict[datetime, WeatherHour], t: datetime) -> tuple[datetime, datetime] | None:
        utc = _utc(t)
        lo = utc.replace(minute=0, second=0, microsecond=0)
        hi = lo if utc == lo else lo + timedelta(hours=1)
        return (lo, hi) if lo in marks and hi in marks else None

    def _covers(self, marks: dict[datetime, WeatherHour], times: Sequence[datetime]) -> bool:
        return all(self._bracket(marks, t) is not None for t in times)

    def _at(self, marks: dict[datetime, WeatherHour], t: datetime) -> WeatherHour:
        bracket = self._bracket(marks, t)
        if bracket is None:
            raise ForecastWeatherError(f"open-meteo response does not cover {t.isoformat()}")
        lo, hi = bracket
        w = 0.0 if lo == hi else (_utc(t) - lo).total_seconds() / 3600.0
        a, b = marks[lo], marks[hi]

        def mix(x: float | None, y: float | None) -> float | None:
            if x is None or y is None:
                return x if y is None else y
            return x + (y - x) * w

        cloud = a.cloud_cover_pct + (b.cloud_cover_pct - a.cloud_cover_pct) * w
        return WeatherHour(
            time=t,
            cloud_cover_pct=max(0.0, min(100.0, cloud)),
            precipitation_mm=mix(a.precipitation_mm, b.precipitation_mm),
            temperature_c=mix(a.temperature_c, b.temperature_c),
        )

    async def _fetch(self, key: tuple[float, float]) -> dict[datetime, WeatherHour]:
        params = {
            "latitude": f"{key[0]:.2f}",
            "longitude": f"{key[1]:.2f}",
            "hourly": "cloud_cover,temperature_2m,precipitation",
            "timezone": "UTC",
            # Today 00:00 UTC through 3 more days: covers now + 48 h whatever the hour.
            "forecast_days": "4",
        }
        try:
            if self._client is not None:
                response = await self._client.get(self._url, params=params, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(self._url, params=params)
            response.raise_for_status()
            return self._parse(response.json())
        except ForecastWeatherError:
            raise
        except httpx.HTTPError as exc:
            raise ForecastWeatherError(f"open-meteo request failed: {type(exc).__name__}") from exc
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ForecastWeatherError(f"open-meteo response unusable: {type(exc).__name__}") from exc

    @staticmethod
    def _parse(payload: object) -> dict[datetime, WeatherHour]:
        hourly = payload["hourly"]  # type: ignore[index]
        stamps = hourly["time"]
        clouds = hourly["cloud_cover"]
        temps = hourly.get("temperature_2m") or [None] * len(stamps)
        rain = hourly.get("precipitation") or [None] * len(stamps)
        if not (len(stamps) == len(clouds) == len(temps) == len(rain)) or not stamps:
            raise ForecastWeatherError("open-meteo hourly arrays are empty or misaligned")
        marks: dict[datetime, WeatherHour] = {}
        for stamp, cloud, temp, precip in zip(stamps, clouds, temps, rain):
            if cloud is None:
                continue  # a gap; coverage is checked per requested hour
            when = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            marks[when] = WeatherHour(
                time=when,
                cloud_cover_pct=max(0.0, min(100.0, float(cloud))),
                precipitation_mm=None if precip is None else max(0.0, float(precip)),
                temperature_c=None if temp is None else float(temp),
            )
        if not marks:
            raise ForecastWeatherError("open-meteo returned no usable hours")
        return marks
