"""
Solar physics for the forecast engine: sun position, clear sky, cloud, temperature, clipping.

This is a deliberately small re-implementation of the pieces of `simulator/engines/sun.py` and
`solar.py` that a forecast needs. The backend cannot import `simulator` (it is a separate
deployable), and copying only what is used keeps the engine auditable. Pure functions, no I/O.

Model, per instant:

    GHI(clear)  = Haurwitz clear sky x turbidity
    GHI         = GHI(clear) x Kasten-Czeplak cloud transmission x rain factor
    beam, diff  = split from cloud cover (overcast kills the beam first), then transposed onto the
                  tilted plane (isotropic sky + albedo + glass reflection)
    DC          = kWp x POA/1000 x performance_ratio x temperature_factor
    AC          = DC x inverter efficiency, clipped at the inverter rating

`performance_ratio` here is the NON-thermal system derate (wiring, mismatch, soiling, shading,
ageing). Temperature is modelled explicitly, so it must not also be baked into the ratio.

Azimuths are compass degrees (0 = north, 180 = south). Kept honest: no spectral, shading-by-horizon
or soiling-dynamics model; calibration (see `calibration.py`) absorbs their average effect.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SOLAR_CONSTANT_WM2 = 1361.0
_SUNRISE_ZENITH_DEG = 90.833

DEFAULT_PERFORMANCE_RATIO = 0.82
DEFAULT_AMBIENT_C = 30.0
DEFAULT_WIND_KMH = 7.0
CLEAR_SKY_TURBIDITY = 0.92
TEMPERATURE_COEFFICIENT_PER_C = 0.0035  # mono-PERC, about -0.35 %/degC above 25 degC cell temperature
INVERTER_EFFICIENCY = 0.97
# Below this share of the rating the inverter is asleep / waking, as the simulated plant does.
_STANDBY_FRACTION = 0.005
_SUBSAMPLES_PER_HOUR = 4


@dataclass(frozen=True, slots=True)
class ArrayParams:
    """One home's array. `tilt_deg` / `azimuth_deg` default to a latitude-tilted equator-facing roof."""

    latitude: float
    longitude: float
    capacity_kwp: float
    inverter_kw: float
    performance_ratio: float = DEFAULT_PERFORMANCE_RATIO
    tilt_deg: float | None = None
    azimuth_deg: float | None = None

    @property
    def effective_tilt(self) -> float:
        return self.tilt_deg if self.tilt_deg is not None else min(abs(self.latitude), 30.0)

    @property
    def effective_azimuth(self) -> float:
        if self.azimuth_deg is not None:
            return self.azimuth_deg
        return 180.0 if self.latitude >= 0 else 0.0

    def with_performance_ratio(self, performance_ratio: float) -> "ArrayParams":
        return ArrayParams(
            self.latitude,
            self.longitude,
            self.capacity_kwp,
            self.inverter_kw,
            performance_ratio,
            self.tilt_deg,
            self.azimuth_deg,
        )


@dataclass(frozen=True, slots=True)
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


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _orbit(ts_utc: datetime) -> tuple[float, float]:
    """(equation of time in minutes, declination in radians), NOAA series."""
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


def solar_position(ts: datetime, latitude: float, longitude: float) -> SunPosition:
    """Sun zenith/azimuth. Longitude and the equation of time are included, so solar noon in
    Pune is ~12:30 IST rather than 12:00."""
    utc = _as_utc(ts)
    eqtime, decl = _orbit(utc)
    minutes = utc.hour * 60.0 + utc.minute + utc.second / 60.0
    true_solar_minutes = minutes + eqtime + 4.0 * longitude
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = min(1.0, max(-1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    denom = math.cos(lat) * math.sin(zenith)
    if abs(denom) < 1e-9:
        azimuth = 180.0 if latitude >= 0 else 0.0
    else:
        cos_az = (math.sin(decl) - math.sin(lat) * cos_zenith) / denom
        azimuth = math.degrees(math.acos(min(1.0, max(-1.0, cos_az))))
        if math.atan2(math.sin(hour_angle), math.cos(hour_angle)) > 0:
            azimuth = 360.0 - azimuth
    return SunPosition(zenith_deg=math.degrees(zenith), azimuth_deg=azimuth)


def air_mass(zenith_deg: float) -> float:
    """Kasten-Young relative air mass."""
    if zenith_deg >= 90.0:
        return 38.0
    return 1.0 / (math.cos(math.radians(zenith_deg)) + 0.50572 * (96.07995 - zenith_deg) ** -1.6364)


@dataclass(frozen=True, slots=True)
class ClearSky:
    ghi: float
    dni: float
    dhi: float


def clear_sky(sun: SunPosition, turbidity_factor: float = CLEAR_SKY_TURBIDITY) -> ClearSky:
    """Haurwitz GHI with a Meinel DNI; South-Asian air is hazy, hence the turbidity factor."""
    cos_z = sun.cos_zenith
    if cos_z <= 0.0:
        return ClearSky(0.0, 0.0, 0.0)
    ghi = 1098.0 * cos_z * math.exp(-0.057 / max(cos_z, 1e-3)) * turbidity_factor
    dni = SOLAR_CONSTANT_WM2 * 0.7 ** (air_mass(sun.zenith_deg) ** 0.678) * turbidity_factor
    dni = min(dni, ghi / max(cos_z, 1e-3))
    dhi = max(0.05 * ghi, ghi - dni * cos_z)
    return ClearSky(ghi=ghi, dni=dni, dhi=dhi)


def cloud_factor(cloud_cover_percent: float) -> float:
    """Kasten-Czeplak global transmission under cloud: 1.0 clear, 0.25 overcast."""
    c = max(0.0, min(1.0, cloud_cover_percent / 100.0))
    return 1.0 - 0.75 * c**3.4


def rain_factor(precipitation_mm: float) -> float:
    return max(0.30, 1.0 - 0.08 * max(0.0, precipitation_mm))


def _cos_incidence(sun: SunPosition, tilt_deg: float, surface_azimuth_deg: float) -> float:
    zen = math.radians(sun.zenith_deg)
    tilt = math.radians(tilt_deg)
    rel = math.radians(sun.azimuth_deg - surface_azimuth_deg)
    return math.cos(zen) * math.cos(tilt) + math.sin(zen) * math.sin(tilt) * math.cos(rel)


def _incidence_angle_modifier(cos_aoi: float, b0: float = 0.05) -> float:
    """ASHRAE glass-reflection loss; zero beyond ~85 degrees incidence."""
    if cos_aoi <= 0.087:
        return 0.0
    return max(0.0, 1.0 - b0 * (1.0 / cos_aoi - 1.0))


def plane_of_array_wm2(
    sun: SunPosition,
    array: ArrayParams,
    *,
    cloud_cover_percent: float,
    precipitation_mm: float,
) -> float:
    """Plane-of-array irradiance under the given sky. 0 when the sun is down."""
    if not sun.is_up:
        return 0.0
    sky = clear_sky(sun)
    if sky.ghi <= 0.0:
        return 0.0
    cloud = max(0.0, min(1.0, cloud_cover_percent / 100.0))
    ghi = sky.ghi * cloud_factor(cloud_cover_percent) * rain_factor(precipitation_mm)
    # Overcast removes the beam much faster than it removes total light; rain removes most of it.
    beam_fraction = (1.0 - cloud) ** 1.4 * (0.2 if precipitation_mm > 0 else 1.0)
    dni = sky.dni * beam_fraction
    dhi = max(0.0, ghi - dni * sun.cos_zenith)
    tilt = array.effective_tilt
    cos_aoi = _cos_incidence(sun, tilt, array.effective_azimuth)
    beam = dni * max(0.0, cos_aoi) * _incidence_angle_modifier(cos_aoi)
    tilt_rad = math.radians(tilt)
    sky_diffuse = dhi * (1.0 + math.cos(tilt_rad)) / 2.0
    ground = ghi * 0.2 * (1.0 - math.cos(tilt_rad)) / 2.0
    return beam + 0.95 * (sky_diffuse + ground)


def cell_temperature_c(poa_wm2: float, ambient_c: float, wind_kmh: float = DEFAULT_WIND_KMH) -> float:
    """Sandia open-rack model."""
    wind_ms = max(0.0, wind_kmh) / 3.6
    module = poa_wm2 * math.exp(-3.56 - 0.075 * wind_ms) + ambient_c
    return module + poa_wm2 / 1000.0 * 3.0


def temperature_factor(cell_c: float) -> float:
    return min(1.10, max(0.60, 1.0 - TEMPERATURE_COEFFICIENT_PER_C * (cell_c - 25.0)))


def physics_power_kw(
    ts: datetime,
    array: ArrayParams,
    *,
    cloud_cover_percent: float = 0.0,
    precipitation_mm: float = 0.0,
    ambient_c: float | None = None,
    clip: bool = True,
) -> float:
    """Instantaneous AC power. `clip=False` returns the un-clipped AC, which calibration needs to
    recognise (and skip) samples the inverter was clipping."""
    sun = solar_position(ts, array.latitude, array.longitude)
    poa = plane_of_array_wm2(
        sun, array, cloud_cover_percent=cloud_cover_percent, precipitation_mm=precipitation_mm
    )
    if poa <= 0.0:
        return 0.0
    ambient = DEFAULT_AMBIENT_C if ambient_c is None else ambient_c
    temp_f = temperature_factor(cell_temperature_c(poa, ambient))
    dc_kw = array.capacity_kwp * poa / 1000.0 * array.performance_ratio * temp_f
    ac_kw = max(0.0, dc_kw * INVERTER_EFFICIENCY)
    if ac_kw < _STANDBY_FRACTION * array.inverter_kw:
        return 0.0
    return min(ac_kw, array.inverter_kw) if clip else ac_kw


def slot_power_kw(
    slot_start: datetime,
    array: ArrayParams,
    *,
    cloud_cover_percent: float,
    precipitation_mm: float = 0.0,
    ambient_c: float | None = None,
) -> float:
    """Mean AC power over the hour starting at `slot_start` (kWh for the slot, numerically).

    Four sub-samples rather than the midpoint: in the sunrise / sunset hours the sun is up for only
    part of the slot, and a single midpoint sample would be all-or-nothing."""
    step = timedelta(minutes=60 / _SUBSAMPLES_PER_HOUR)
    first = slot_start + step / 2
    total = sum(
        physics_power_kw(
            first + step * i,
            array,
            cloud_cover_percent=cloud_cover_percent,
            precipitation_mm=precipitation_mm,
            ambient_c=ambient_c,
        )
        for i in range(_SUBSAMPLES_PER_HOUR)
    )
    return total / _SUBSAMPLES_PER_HOUR
