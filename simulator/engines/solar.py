"""
Solar PV production from weather.

    POA irradiance  = transpose(GHI, DNI, DHI) onto the tilted array
                      (isotropic sky + ground albedo + glass reflection loss)
    DC power        = kWp × POA/1000
                      × solar_efficiency    (wiring, mismatch, LID, nameplate)
                      × shading_factor
                      × temperature_factor  (cell temperature, SAPM open rack)
                      × (1 − soiling_loss)  (dust; washed by rain / cleaning)
                      × ageing              ((1 − degradation)^age_years)
    AC power        = DC − inverter losses, clipped to inverter_capacity_kw

Cloud shadows: hourly weather is smooth, real partly-cloudy skies are not.
The beam component is switched between "sun" and "cloud shadow" on a ~75 s
lattice whose sunny probability equals DNI / clear-sky DNI, so the hourly mean
is preserved while minute-scale ramps appear. Clear and fully overcast skies
stay steady. Neighbouring homes share the same cloud field, time-shifted.

If irradiance is missing (sensor drop-out) a clear-sky × cloud estimate is
used — the panels keep producing even when the weather sensor fails.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.engines import randomness as rnd
from simulator.engines.sun import (
    SunPosition,
    cell_temperature_c,
    clear_sky,
    plane_of_array,
    solar_position,
)
from simulator.models import WeatherRecord

_SHADOW_LATTICE_SECONDS = 75.0
_SHADOW_BEAM_FRACTION = 0.05


def temperature_factor(cell_temperature: float, coefficient: float) -> float:
    """STC is 25 °C cell temperature. Mono-PERC is about −0.35 %/°C."""
    factor = 1.0 - coefficient * (cell_temperature - 25.0)
    return min(1.10, max(0.60, factor))


def cloud_factor(cloud_cover_percent: float) -> float:
    """Kasten–Czeplak global transmission under cloud."""
    c = max(0.0, min(1.0, cloud_cover_percent / 100.0))
    return 1.0 - 0.75 * c**3.4


def rain_factor(precipitation_mm: float) -> float:
    return max(0.30, 1.0 - 0.08 * max(0.0, precipitation_mm))


def inverter_ac_kw(dc_kw: float, rated_kw: float) -> tuple[float, float]:
    """(AC before clipping, clipped kW). Self-consumption + ohmic losses."""
    if rated_kw <= 0 or dc_kw <= max(0.01, 0.004 * rated_kw):
        return 0.0, 0.0
    loss = 0.004 * rated_kw + 0.01 * dc_kw + 0.012 * dc_kw * dc_kw / rated_kw
    ac = max(0.0, dc_kw - loss)
    clipped = max(0.0, ac - rated_kw)
    return ac, clipped


@dataclass
class SolarSnapshot:
    ac_kw: float = 0.0
    dc_kw: float = 0.0
    clipped_kw: float = 0.0
    poa_irradiance_wm2: float = 0.0
    cell_temperature_c: float | None = None
    temperature_loss_percent: float = 0.0
    soiling_loss_percent: float = 0.0
    shading_loss_percent: float = 0.0
    inverter_efficiency_percent: float | None = None
    inverter_status: str = "sleeping"
    cloud_shadow: bool = False
    irradiance_source: str = "measured"
    performance_ratio: float | None = None

    def fields(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("ac_kw")
        return data


class SolarGenerator:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.energy_today_kwh = 0.0
        self.energy_total_kwh = 0.0
        self.peak_today_kw = 0.0
        self._today: date | None = None
        self.soiling_loss = max(0.0, config.initial_soiling_loss_percent / 100.0)
        self.days_since_cleaning = 0
        self.cleaned_today = False
        self._soil_day: date | None = None
        self._rain_today_mm = 0.0
        self.last = SolarSnapshot()

    def reset_daily_if_needed(self, ts: datetime) -> None:
        tz = ZoneInfo(self.config.timezone)
        local_date = ts.astimezone(tz).date()
        if self._today != local_date:
            self._today = local_date
            self.energy_today_kwh = 0.0
            self.peak_today_kw = 0.0

    def _update_soiling(self, local: datetime, weather: WeatherRecord) -> None:
        cfg = self.config
        day = local.date()
        if self._soil_day is None:
            self._soil_day = day
        elif day != self._soil_day:
            elapsed = max(1, (day - self._soil_day).days)
            month = self._soil_day.month
            dusty_season = 1.5 if month in (3, 4, 5) else 1.0
            if self._rain_today_mm >= 5.0:
                self.soiling_loss *= 0.1
            elif self._rain_today_mm >= 1.0:
                self.soiling_loss *= 0.5
            else:
                self.soiling_loss += cfg.soiling_rate_per_day * dusty_season * elapsed
            self.soiling_loss = min(cfg.soiling_max_loss, self.soiling_loss)
            self.days_since_cleaning += elapsed
            self.cleaned_today = False
            interval = cfg.panel_cleaning_interval_days
            if interval > 0 and self.days_since_cleaning >= interval:
                self.soiling_loss *= 0.1
                self.days_since_cleaning = 0
                self.cleaned_today = True
            self._rain_today_mm = 0.0
            self._soil_day = day
        self._rain_today_mm += max(0.0, weather.precipitation_mm) * cfg.interval_hours

    def _beam_with_shadows(
        self, local: datetime, sun: SunPosition, dni: float, clear_dni: float
    ) -> tuple[float, bool]:
        if not self.config.cloud_transients or clear_dni < 50.0:
            return dni, False
        ratio = max(0.0, min(1.0, dni / clear_dni))
        sunny_probability = (ratio - _SHADOW_BEAM_FRACTION) / (1.0 - _SHADOW_BEAM_FRACTION)
        if sunny_probability <= 0.03 or sunny_probability >= 0.97:
            return dni, False
        cfg = self.config
        shift = rnd.uniform(cfg.household_seed, "cloud-shift") * 120.0
        position = (local.timestamp() + shift) / _SHADOW_LATTICE_SECONDS
        index = math.floor(position)
        frac = position - index

        def sunny(k: int) -> float:
            return 1.0 if rnd.uniform(cfg.location_seed, "cloud-field", k) < sunny_probability else 0.0

        s0 = sunny(index)
        s1 = sunny(index + 1)
        # Cloud edges take ~15 s to sweep across a rooftop array.
        edge = max(0.0, (frac - 0.8) / 0.2)
        level = s0 + (s1 - s0) * (edge * edge * (3 - 2 * edge))
        beam = clear_dni * (_SHADOW_BEAM_FRACTION + (1.0 - _SHADOW_BEAM_FRACTION) * level)
        return beam, level < 0.5

    def compute(
        self, ts: datetime, weather: WeatherRecord, scenario: str | None = None
    ) -> SolarSnapshot:
        cfg = self.config
        scenario = scenario or cfg.scenario
        tz = ZoneInfo(cfg.timezone)
        local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
        self._update_soiling(local, weather)

        soiling = self.soiling_loss
        if scenario == "dusty_panels":
            soiling = max(soiling, 0.18)

        sun = solar_position(local, cfg.latitude, cfg.longitude)
        if not sun.is_up:
            self.last = SolarSnapshot(soiling_loss_percent=round(soiling * 100.0, 2))
            return self.last

        elevation = float(cfg.location_elevation_m or 0.0)
        sky = clear_sky(sun, elevation, cfg.clear_sky_turbidity)
        ghi = weather.shortwave_radiation_wm2
        measured = ghi > 0.0
        if measured:
            if weather.direct_normal_irradiance_wm2 is not None and weather.direct_normal_irradiance_wm2 > 0:
                dni = weather.direct_normal_irradiance_wm2
            else:
                dni = weather.direct_radiation_wm2 / sun.cos_zenith if sun.cos_zenith > 0.05 else 0.0
            dhi = weather.diffuse_radiation_wm2
            source = "measured"
        else:
            # Weather sensor missing: estimate from clear sky and cloud cover.
            c = max(0.0, min(1.0, weather.cloud_cover_percent / 100.0))
            ghi = sky.ghi * cloud_factor(weather.cloud_cover_percent) * rain_factor(weather.precipitation_mm)
            beam_fraction = max(0.0, 1.0 - c) ** 1.4 * (0.2 if weather.precipitation_mm > 0 else 1.0)
            dni = sky.dni * beam_fraction
            dhi = max(0.0, ghi - dni * sun.cos_zenith)
            source = "estimated"

        dni = min(dni, sky.dni * 1.05) if sky.dni > 0 else dni
        beam, shadow = self._beam_with_shadows(local, sun, dni, sky.dni)
        poa = plane_of_array(
            ghi=ghi,
            dni=beam,
            dhi=dhi,
            sun=sun,
            tilt_deg=cfg.tilt_deg,
            surface_azimuth_deg=cfg.azimuth_deg,
            albedo=cfg.ground_albedo,
        )
        beam_poa, diffuse_poa = poa.beam, poa.diffuse
        shading_f = cfg.shading_factor
        if scenario == "partial_shading":
            facing = abs(((sun.azimuth_deg - cfg.azimuth_deg + 180.0) % 360.0) - 180.0)
            if sun.elevation_deg < 28.0 or facing > 65.0:
                unshaded = beam_poa + diffuse_poa
                # Bypass diodes drop whole substrings, so a small shadow costs a lot.
                beam_poa *= 0.3
                diffuse_poa *= 0.9
                if unshaded > 0:
                    shading_f *= (beam_poa + diffuse_poa) / unshaded
        poa_total = beam_poa + diffuse_poa

        cell_t = cell_temperature_c(poa_total, weather.temperature_c, weather.wind_speed_kmh)
        temp_f = temperature_factor(cell_t, cfg.pv_temp_coefficient_per_c)
        age_f = (1.0 - cfg.panel_degradation_per_year) ** max(0.0, cfg.panel_age_years)
        jitter = 1.0 + 0.02 * (rnd.smooth_noise(local.timestamp(), 240.0, cfg.household_seed, "pv-jitter") - 0.5)

        dc_kw = (
            cfg.solar_capacity_kwp
            * poa_total
            / 1000.0
            * cfg.solar_efficiency
            * cfg.shading_factor
            * temp_f
            * (1.0 - soiling)
            * age_f
            * jitter
        )
        dc_kw = max(0.0, dc_kw)
        ac_raw, clipped = inverter_ac_kw(dc_kw, cfg.inverter_capacity_kw)
        ac_kw = min(ac_raw, cfg.inverter_capacity_kw)

        if ac_kw <= 0.0:
            status = "waking" if dc_kw > 0 else "sleeping"
        elif clipped > 0.01:
            status = "clipping"
        else:
            status = "mppt"

        # PR against unshaded plane-of-array irradiance, so shading and
        # soiling show up as a low ratio (what a monitoring portal reports).
        reference = cfg.solar_capacity_kwp * poa.total / 1000.0
        self.last = SolarSnapshot(
            ac_kw=ac_kw,
            dc_kw=round(dc_kw, 4),
            clipped_kw=round(clipped, 4),
            poa_irradiance_wm2=round(poa.total, 1),
            cell_temperature_c=round(cell_t, 2),
            temperature_loss_percent=round(max(0.0, 1.0 - temp_f) * 100.0, 2),
            soiling_loss_percent=round(soiling * 100.0, 2),
            shading_loss_percent=round(max(0.0, 1.0 - shading_f) * 100.0, 2),
            inverter_efficiency_percent=round(ac_raw / dc_kw * 100.0, 2) if dc_kw > 0 and ac_raw > 0 else None,
            inverter_status=status,
            cloud_shadow=shadow,
            irradiance_source=source,
            performance_ratio=round(ac_kw / reference, 3) if reference > 0.05 else None,
        )
        return self.last

    def compute_power_kw(
        self, ts: datetime, weather: WeatherRecord, scenario: str | None = None
    ) -> float:
        return self.compute(ts, weather, scenario).ac_kw

    def accumulate(self, ts: datetime, power_kw: float) -> dict[str, float]:
        """Record delivered (post-dispatch) solar energy for this interval."""
        self.reset_daily_if_needed(ts)
        power_kw = max(0.0, power_kw)
        interval_kwh = power_kw * self.config.interval_hours
        self.energy_today_kwh += interval_kwh
        self.energy_total_kwh += interval_kwh
        self.peak_today_kw = max(self.peak_today_kw, power_kw)
        return {
            "solar_power_kw": round(power_kw, 4),
            "solar_energy_interval_kwh": round(interval_kwh, 6),
            "solar_energy_today_kwh": round(self.energy_today_kwh, 6),
            "solar_energy_total_kwh": round(self.energy_total_kwh, 6),
        }

    def state(self) -> dict[str, Any]:
        return {
            "energy_today_kwh": self.energy_today_kwh,
            "energy_total_kwh": self.energy_total_kwh,
            "peak_today_kw": self.peak_today_kw,
            "today": self._today.isoformat() if self._today else None,
            "soiling_loss": self.soiling_loss,
            "days_since_cleaning": self.days_since_cleaning,
            "soil_day": self._soil_day.isoformat() if self._soil_day else None,
            "rain_today_mm": self._rain_today_mm,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.energy_today_kwh = float(data.get("energy_today_kwh", 0.0))
        self.energy_total_kwh = float(data.get("energy_total_kwh", 0.0))
        self.peak_today_kw = float(data.get("peak_today_kw", 0.0))
        self._today = date.fromisoformat(data["today"]) if data.get("today") else None
        self.soiling_loss = float(data.get("soiling_loss", self.soiling_loss))
        self.days_since_cleaning = int(data.get("days_since_cleaning", 0))
        self._soil_day = date.fromisoformat(data["soil_day"]) if data.get("soil_day") else None
        self._rain_today_mm = float(data.get("rain_today_mm", 0.0))
