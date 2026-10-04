"""
Battery engine (home LFP pack + hybrid inverter BMS behaviour).

Charge and discharge are tracked as separate non-negative kW fields.
SOC persists between ticks. Efficiency is applied on energy, not on the
ambiguous signed-power convention.

Real-world behaviour modelled:
- Usable capacity shrinks with state of health (SOH).
- CC/CV taper: charge power falls off above `battery_taper_start_soc`.
- Discharge derates in the last few % above the reserve.
- Thermal mass: temperature moves toward (enclosure ambient + I²R heating)
  with a ~90 min time constant instead of jumping each tick.
- BMS temperature derate and an over-temperature fault (code 2) that clears
  after cooling; low-temperature charge block (code 0, derate only).
- SOH falls with equivalent full cycles (cycle_life cycles → 80 %).
- A home without a battery reports SOC 0, status "unavailable", no temperature.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from simulator.config import SimulatorConfig
from simulator.models import WeatherRecord

FAULT_OVER_TEMPERATURE = 2


class BatteryEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.soc_percent = (
            min(100.0, max(0.0, config.initial_battery_soc_percent)) if config.battery_present else 0.0
        )
        self.soh_percent = config.battery_soh_percent if config.battery_present else 0.0
        self.fault_code = 0
        self.temperature_c: float | None = None
        self.throughput_kwh = 0.0
        self.charge_today_kwh = 0.0
        self.discharge_today_kwh = 0.0
        self._today: date | None = None
        self.derate_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.config.battery_present and self.fault_code == 0

    def soc_capacity_kwh(self) -> float:
        """Usable tank for SOC: usable (≤ nameplate) scaled by SOH."""
        nameplate = max(self.config.battery_capacity_kwh, 1e-6)
        usable = self.config.battery_usable_capacity_kwh
        if usable <= 0:
            usable = nameplate
        health = max(0.3, self.soh_percent / 100.0) if self.config.battery_present else 1.0
        return max(1e-6, min(usable, nameplate) * health)

    def energy_available_kwh(self, floor_soc: float | None = None) -> float:
        if not self.available:
            return 0.0
        floor = self.config.battery_minimum_soc_percent if floor_soc is None else floor_soc
        headroom = max(0.0, self.soc_percent - floor)
        return headroom / 100.0 * self.soc_capacity_kwh()

    def energy_headroom_kwh(self) -> float:
        if not self.available:
            return 0.0
        return max(0.0, (100.0 - self.soc_percent) / 100.0 * self.soc_capacity_kwh())

    def _thermal_derate(self, charging: bool) -> float:
        temp = self.temperature_c
        if temp is None:
            return 1.0
        limit = self.config.battery_max_temperature_c
        if temp >= limit - 10.0:
            return max(0.3, 1.0 - 0.07 * (temp - (limit - 10.0)))
        if charging and temp < 5.0:
            return 0.2 if temp > 0.0 else 0.0
        return 1.0

    def max_charge_kw(self) -> float:
        if not self.available:
            return 0.0
        dt_h = self.config.interval_hours
        if dt_h <= 0:
            return 0.0
        energy_cap = self.energy_headroom_kwh() / max(self.config.battery_charge_efficiency, 1e-6)
        power_cap = energy_cap / dt_h
        limit = self.config.battery_max_charge_power_kw
        taper_start = min(99.0, self.config.battery_taper_start_soc)
        if self.soc_percent > taper_start:
            limit *= max(0.05, (100.0 - self.soc_percent) / (100.0 - taper_start))
        limit *= self._thermal_derate(charging=True)
        return max(0.0, min(limit, power_cap))

    def max_discharge_kw(self, floor_soc: float | None = None) -> float:
        if not self.available:
            return 0.0
        dt_h = self.config.interval_hours
        if dt_h <= 0:
            return 0.0
        floor = self.config.battery_minimum_soc_percent if floor_soc is None else floor_soc
        energy_ac = self.energy_available_kwh(floor) * self.config.battery_discharge_efficiency
        power_cap = energy_ac / dt_h
        limit = self.config.battery_max_discharge_power_kw
        band = self.soc_percent - floor
        if band < 5.0:
            limit *= max(0.2, band / 5.0)
        limit *= self._thermal_derate(charging=False)
        return max(0.0, min(limit, power_cap))

    def _reset_daily(self, ts: datetime | None) -> None:
        if ts is None:
            return
        day = ts.date()
        if self._today != day:
            self._today = day
            self.charge_today_kwh = 0.0
            self.discharge_today_kwh = 0.0

    def _update_temperature(
        self, weather: WeatherRecord, charge_kw: float, discharge_kw: float, scenario: str
    ) -> None:
        cfg = self.config
        enclosure = 0.5 * weather.temperature_c + 0.5 * 28.0
        heating_gain = 4.0
        if scenario == "battery_overheat":
            # Poorly ventilated, sun-exposed enclosure: the BMS derate alone
            # cannot hold the pack under the trip limit on a hot afternoon.
            enclosure += 10.0 + 0.012 * weather.shortwave_radiation_wm2
            heating_gain = 18.0
        if self.soh_percent < 80.0:
            heating_gain *= 1.5
        rated = max(cfg.battery_max_charge_power_kw, cfg.battery_max_discharge_power_kw, 1e-6)
        load = (charge_kw + discharge_kw) / rated
        target = enclosure + heating_gain * load * load + 1.0
        if self.temperature_c is None:
            self.temperature_c = enclosure + 1.0
        tau = max(60.0, cfg.battery_thermal_time_constant_minutes * 60.0)
        alpha = 1.0 - math.exp(-cfg.simulation_interval_seconds / tau)
        self.temperature_c += (target - self.temperature_c) * alpha

        limit = cfg.battery_max_temperature_c
        if self.fault_code == 0 and self.temperature_c >= limit:
            self.fault_code = FAULT_OVER_TEMPERATURE
        elif self.fault_code == FAULT_OVER_TEMPERATURE and self.temperature_c < limit - 8.0:
            self.fault_code = 0

    def apply(
        self,
        charge_kw: float,
        discharge_kw: float,
        weather: WeatherRecord,
        scenario: str | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        cfg = self.config
        scenario = scenario or cfg.scenario
        self._reset_daily(ts or weather.timestamp)
        charge_kw = max(0.0, charge_kw)
        discharge_kw = max(0.0, discharge_kw)
        if charge_kw > 0 and discharge_kw > 0:
            # Never charge and discharge in the same interval.
            if charge_kw >= discharge_kw:
                discharge_kw = 0.0
            else:
                charge_kw = 0.0

        if not cfg.battery_present:
            return self._absent_fields()

        if not self.available:
            charge_kw = 0.0
            discharge_kw = 0.0

        dt_h = cfg.interval_hours
        capacity = self.soc_capacity_kwh()
        charge_kwh = charge_kw * dt_h
        discharge_kwh = discharge_kw * dt_h

        if charge_kw > 0:
            added_kwh = charge_kwh * cfg.battery_charge_efficiency
            self.soc_percent = min(100.0, self.soc_percent + 100.0 * added_kwh / capacity)
        if discharge_kw > 0:
            removed_kwh = discharge_kwh / max(cfg.battery_discharge_efficiency, 1e-6)
            min_soc = cfg.battery_minimum_soc_percent
            self.soc_percent = max(min_soc, self.soc_percent - 100.0 * removed_kwh / capacity)
            self.throughput_kwh += removed_kwh
            nameplate = max(cfg.battery_capacity_kwh, 1e-6)
            fade_per_cycle = 20.0 / max(1, cfg.battery_cycle_life)
            self.soh_percent = max(50.0, self.soh_percent - fade_per_cycle * removed_kwh / nameplate)

        self.charge_today_kwh += charge_kwh
        self.discharge_today_kwh += discharge_kwh
        self._update_temperature(weather, charge_kw, discharge_kw, scenario)

        derate = self._thermal_derate(charging=True)
        if self.fault_code:
            self.derate_reason = "over_temperature_fault"
        elif derate < 1.0 and self.temperature_c is not None and self.temperature_c > 30:
            self.derate_reason = "high_temperature"
        elif derate < 1.0:
            self.derate_reason = "low_temperature"
        elif self.soc_percent > cfg.battery_taper_start_soc:
            self.derate_reason = "cv_taper"
        elif self.soc_percent - cfg.battery_minimum_soc_percent < 5.0:
            self.derate_reason = "near_reserve"
        else:
            self.derate_reason = None

        status = self._status(charge_kw, discharge_kw)
        nameplate = max(cfg.battery_capacity_kwh, 1e-6)
        return {
            "battery_soc_percent": round(self.soc_percent, 3),
            "battery_soh_percent": round(self.soh_percent, 3),
            "battery_power_kw": round(charge_kw - discharge_kw, 4),
            "battery_charge_power_kw": round(charge_kw, 4),
            "battery_discharge_power_kw": round(discharge_kw, 4),
            "battery_charge_interval_kwh": round(charge_kwh, 6),
            "battery_discharge_interval_kwh": round(discharge_kwh, 6),
            "battery_charge_today_kwh": round(self.charge_today_kwh, 4),
            "battery_discharge_today_kwh": round(self.discharge_today_kwh, 4),
            "battery_energy_available_kwh": round(self.energy_available_kwh(), 4),
            "battery_capacity_effective_kwh": round(capacity, 3),
            "battery_cycle_count": round(self.throughput_kwh / nameplate, 2),
            "battery_temperature_c": round(self.temperature_c, 2) if self.temperature_c is not None else None,
            "battery_status": status,
            "battery_fault_code": self.fault_code,
            "battery_derate_reason": self.derate_reason,
            "battery_present": True,
        }

    def _absent_fields(self) -> dict[str, Any]:
        return {
            "battery_soc_percent": 0.0,
            "battery_soh_percent": 0.0,
            "battery_power_kw": 0.0,
            "battery_charge_power_kw": 0.0,
            "battery_discharge_power_kw": 0.0,
            "battery_charge_interval_kwh": 0.0,
            "battery_discharge_interval_kwh": 0.0,
            "battery_charge_today_kwh": 0.0,
            "battery_discharge_today_kwh": 0.0,
            "battery_energy_available_kwh": 0.0,
            "battery_capacity_effective_kwh": 0.0,
            "battery_cycle_count": 0.0,
            "battery_temperature_c": None,
            "battery_status": "unavailable",
            "battery_fault_code": 0,
            "battery_derate_reason": None,
            "battery_present": False,
        }

    def _status(self, charge_kw: float, discharge_kw: float) -> str:
        if not self.config.battery_present:
            return "unavailable"
        if self.fault_code:
            return "fault"
        if charge_kw > 0.02:
            return "charging"
        if discharge_kw > 0.02:
            return "discharging"
        if self.soc_percent >= 99.5:
            return "full"
        if self.soc_percent <= self.config.battery_minimum_soc_percent + 0.05:
            return "idle_min_soc"
        return "idle"

    def state(self) -> dict[str, Any]:
        return {
            "soc_percent": self.soc_percent,
            "soh_percent": self.soh_percent,
            "fault_code": self.fault_code,
            "temperature_c": self.temperature_c,
            "throughput_kwh": self.throughput_kwh,
            "charge_today_kwh": self.charge_today_kwh,
            "discharge_today_kwh": self.discharge_today_kwh,
            "today": self._today.isoformat() if self._today else None,
        }

    def restore(self, data: dict[str, Any], *, keep_soc: bool = False) -> None:
        if not self.config.battery_present:
            return
        if not keep_soc:
            self.soc_percent = min(100.0, max(0.0, float(data.get("soc_percent", self.soc_percent))))
        # A configured lower SOH (e.g. battery_degraded) wins over the saved one.
        self.soh_percent = min(self.soh_percent, float(data.get("soh_percent", self.soh_percent)))
        self.fault_code = int(data.get("fault_code", 0))
        temp = data.get("temperature_c")
        self.temperature_c = float(temp) if temp is not None else None
        self.throughput_kwh = float(data.get("throughput_kwh", 0.0))
        self.charge_today_kwh = float(data.get("charge_today_kwh", 0.0))
        self.discharge_today_kwh = float(data.get("discharge_today_kwh", 0.0))
        self._today = date.fromisoformat(data["today"]) if data.get("today") else None
