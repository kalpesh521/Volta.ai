"""
Solar geometry and irradiance physics.

- Sun position / sunrise / sunset: NOAA general solar position equations
  (accounts for longitude and the equation of time, so solar noon in Pune is
  ~12:30 IST, not 12:00).
- Clear sky: Haurwitz GHI + Meinel DNI with an altitude correction, scaled by
  a turbidity factor (South-Asian air is hazy).
- Plane of array: isotropic sky (Liu–Jordan) + ground albedo + ASHRAE
  incidence-angle modifier for glass reflection.
- Cell temperature: Sandia (SAPM) open-rack glass/polymer model.

Azimuths are compass degrees: 0 = north, 90 = east, 180 = south, 270 = west.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SOLAR_CONSTANT_WM2 = 1361.0
_SUNRISE_ZENITH_DEG = 90.833


@dataclass(frozen=True)
class SunPosition:
    zenith_deg: float
    azimuth_deg: float

    @property
    def elevation_deg(self) -> float:
        return 90.0 - self.zenith_deg

    @property
    def cos_zenith(self) -> float:
        return max(0.0, math.cos(math.radians(self.zenith_deg)))

    @property
    def is_up(self) -> bool:
        return self.zenith_deg < _SUNRISE_ZENITH_DEG


def _orbit(ts_utc: datetime) -> tuple[float, float]:
    """(equation of time minutes, declination radians)."""
    day_of_year = ts_utc.timetuple().tm_yday
    hour = ts_utc.hour + ts_utc.minute / 60.0 + ts_utc.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return eqtime, decl


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def solar_position(ts: datetime, latitude: float, longitude: float) -> SunPosition:
    utc = _as_utc(ts)
    eqtime, decl = _orbit(utc)
    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0
    true_solar_minutes = minutes + eqtime + 4.0 * longitude
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    lat = math.radians(latitude)

    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = min(1.0, max(-1.0, cos_zenith))
    zenith = math.acos(cos_zenith)

    sin_zenith = math.sin(zenith)
    denom = math.cos(lat) * sin_zenith
    if abs(denom) < 1e-9:
        azimuth = 180.0 if latitude >= 0 else 0.0
    else:
        cos_az = (math.sin(decl) - math.sin(lat) * cos_zenith) / denom
        azimuth = math.degrees(math.acos(min(1.0, max(-1.0, cos_az))))
        hour_angle_wrapped = math.atan2(math.sin(hour_angle), math.cos(hour_angle))
        if hour_angle_wrapped > 0:
            azimuth = 360.0 - azimuth
    return SunPosition(zenith_deg=math.degrees(zenith), azimuth_deg=azimuth)


def sun_times(day_ts: datetime, latitude: float, longitude: float) -> tuple[datetime, datetime]:
    """Sunrise / sunset for the local calendar day of `day_ts` (tz-aware)."""
    tz = day_ts.tzinfo or timezone.utc
    local_noon = day_ts.replace(hour=12, minute=0, second=0, microsecond=0)
    eqtime, decl = _orbit(_as_utc(local_noon))
    lat = math.radians(latitude)
    arg = math.cos(math.radians(_SUNRISE_ZENITH_DEG)) / (math.cos(lat) * math.cos(decl)) - math.tan(lat) * math.tan(decl)
    if arg >= 1.0:
        # Polar night: collapse to a zero-length day at solar noon.
        arg = 1.0
    arg = max(-1.0, arg)
    half_day_deg = math.degrees(math.acos(arg))
    midnight_utc = datetime(local_noon.year, local_noon.month, local_noon.day, tzinfo=timezone.utc)
    sunrise_min = 720.0 - 4.0 * (longitude + half_day_deg) - eqtime
    sunset_min = 720.0 - 4.0 * (longitude - half_day_deg) - eqtime
    sunrise = (midnight_utc + timedelta(minutes=sunrise_min)).astimezone(tz)
    sunset = (midnight_utc + timedelta(minutes=sunset_min)).astimezone(tz)
    return sunrise.replace(microsecond=0), sunset.replace(microsecond=0)


def air_mass(zenith_deg: float) -> float:
    """Kasten–Young relative air mass."""
    if zenith_deg >= 90.0:
        return 38.0
    return 1.0 / (math.cos(math.radians(zenith_deg)) + 0.50572 * (96.07995 - zenith_deg) ** -1.6364)


@dataclass(frozen=True)
class ClearSky:
    ghi: float
    dni: float
    dhi: float


def clear_sky(sun: SunPosition, elevation_m: float = 0.0, turbidity_factor: float = 0.92) -> ClearSky:
    cos_z = sun.cos_zenith
    if cos_z <= 0.0:
        return ClearSky(0.0, 0.0, 0.0)
    ghi = 1098.0 * cos_z * math.exp(-0.057 / max(cos_z, 1e-3))
    altitude_km = max(0.0, elevation_m) / 1000.0
    am = air_mass(sun.zenith_deg)
    dni = SOLAR_CONSTANT_WM2 * ((1.0 - 0.14 * altitude_km) * 0.7 ** (am**0.678) + 0.14 * altitude_km)
    ghi *= turbidity_factor * (1.0 + 0.04 * altitude_km)
    dni *= turbidity_factor
    dni = min(dni, ghi / max(cos_z, 1e-3))
    dhi = max(0.05 * ghi, ghi - dni * cos_z)
    return ClearSky(ghi=ghi, dni=dni, dhi=dhi)


def cos_incidence(sun: SunPosition, tilt_deg: float, surface_azimuth_deg: float) -> float:
    zen = math.radians(sun.zenith_deg)
    tilt = math.radians(tilt_deg)
    rel = math.radians(sun.azimuth_deg - surface_azimuth_deg)
    return math.cos(zen) * math.cos(tilt) + math.sin(zen) * math.sin(tilt) * math.cos(rel)


def incidence_angle_modifier(cos_aoi: float, b0: float = 0.05) -> float:
    """ASHRAE glass reflection loss; 0 beyond ~85° incidence."""
    if cos_aoi <= 0.087:
        return 0.0
    return max(0.0, 1.0 - b0 * (1.0 / cos_aoi - 1.0))


@dataclass(frozen=True)
class PlaneOfArray:
    beam: float
    diffuse: float

    @property
    def total(self) -> float:
        return self.beam + self.diffuse


def plane_of_array(
    *,
    ghi: float,
    dni: float,
    dhi: float,
    sun: SunPosition,
    tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float = 0.2,
) -> PlaneOfArray:
    if not sun.is_up or ghi <= 0.0:
        return PlaneOfArray(0.0, 0.0)
    cos_aoi = cos_incidence(sun, tilt_deg, surface_azimuth_deg)
    beam = max(0.0, dni) * max(0.0, cos_aoi) * incidence_angle_modifier(cos_aoi)
    tilt = math.radians(tilt_deg)
    sky = max(0.0, dhi) * (1.0 + math.cos(tilt)) / 2.0
    ground = max(0.0, ghi) * albedo * (1.0 - math.cos(tilt)) / 2.0
    # Diffuse light also loses a little to reflection (~5 %).
    return PlaneOfArray(beam=beam, diffuse=0.95 * (sky + ground))


def cell_temperature_c(poa_wm2: float, ambient_c: float, wind_kmh: float) -> float:
    """SAPM open-rack: module back temp + 3 °C conduction delta at 1 kW/m²."""
    wind_ms = max(0.0, wind_kmh) / 3.6
    module = poa_wm2 * math.exp(-3.56 - 0.075 * wind_ms) + ambient_c
    return module + poa_wm2 / 1000.0 * 3.0


def dew_point_c(temperature_c: float, humidity_percent: float) -> float:
    rh = min(100.0, max(1.0, humidity_percent))
    gamma = math.log(rh / 100.0) + 17.625 * temperature_c / (243.04 + temperature_c)
    return 243.04 * gamma / (17.625 - gamma)


def humidity_from_dew_point(temperature_c: float, dew_c: float) -> float:
    def es(t: float) -> float:
        return math.exp(17.625 * t / (243.04 + t))

    return min(100.0, max(5.0, 100.0 * es(dew_c) / es(temperature_c)))


def apparent_temperature_c(temperature_c: float, humidity_percent: float, wind_kmh: float) -> float:
    """Heat index when hot and humid, wind chill when cold and windy, else air temperature."""
    t = temperature_c
    rh = humidity_percent
    if t >= 27.0 and rh >= 40.0:
        tf = t * 9.0 / 5.0 + 32.0
        hi = (
            -42.379
            + 2.04901523 * tf
            + 10.14333127 * rh
            - 0.22475541 * tf * rh
            - 6.83783e-3 * tf * tf
            - 5.481717e-2 * rh * rh
            + 1.22874e-3 * tf * tf * rh
            + 8.5282e-4 * tf * rh * rh
            - 1.99e-6 * tf * tf * rh * rh
        )
        return (hi - 32.0) * 5.0 / 9.0
    if t <= 10.0 and wind_kmh > 4.8:
        v = wind_kmh**0.16
        return 13.12 + 0.6215 * t - 11.37 * v + 0.3965 * t * v
    return t


WMO_CONDITIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    80: "light rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def weather_condition(code: int | None) -> str | None:
    if code is None:
        return None
    return WMO_CONDITIONS.get(int(code), f"wmo_{int(code)}")
