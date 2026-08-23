"""
Battery SOC engine.

Charge and discharge are tracked as separate non-negative kW fields.
SOC persists between ticks. Efficiency is applied on energy, not on the
ambiguous signed-power convention.
"""

from __future__ import annotations

from simulator.config import SimulatorConfig
from simulator.models import WeatherRecord


class BatteryEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.soc_percent = min(100.0, max(0.0, config.initial_battery_soc_percent))
        self.soh_percent = config.battery_soh_percent
        self.fault_code = 0
        self.temperature_c = 28.0

    @property
    def available(self) -> bool:
        return self.config.battery_present and self.fault_code == 0

    def soc_capacity_kwh(self) -> float:
        """Usable tank for SOC. Never larger than nameplate."""
        nameplate = max(self.config.battery_capacity_kwh, 1e-6)
        usable = self.config.battery_usable_capacity_kwh
        if usable <= 0:
            return nameplate
        return min(usable, nameplate)

    def energy_available_kwh(self) -> float:
        if not self.available:
            return 0.0
        headroom = max(0.0, self.soc_percent - self.config.battery_minimum_soc_percent)
        return headroom / 100.0 * self.soc_capacity_kwh()

    def energy_headroom_kwh(self) -> float:
        if not self.available:
            return 0.0
        return max(0.0, (100.0 - self.soc_percent) / 100.0 * self.soc_capacity_kwh())

    def max_charge_kw(self) -> float:
        if not self.available:
            return 0.0
        dt_h = self.config.interval_hours
        if dt_h <= 0:
            return 0.0
        # AC power that can be absorbed this interval without exceeding 100% SOC.
        energy_cap = self.energy_headroom_kwh() / max(self.config.battery_charge_efficiency, 1e-6)
        power_cap = energy_cap / dt_h
        return max(0.0, min(self.config.battery_max_charge_power_kw, power_cap))

    def max_discharge_kw(self) -> float:
        if not self.available:
            return 0.0
        dt_h = self.config.interval_hours
        if dt_h <= 0:
            return 0.0
        # AC power deliverable this interval without dropping below min SOC.
        energy_ac = self.energy_available_kwh() * self.config.battery_discharge_efficiency
        power_cap = energy_ac / dt_h
        return max(0.0, min(self.config.battery_max_discharge_power_kw, power_cap))

    def apply(
        self,
        charge_kw: float,
        discharge_kw: float,
        weather: WeatherRecord,
    ) -> dict[str, float | str | int]:
        charge_kw = max(0.0, charge_kw)
        discharge_kw = max(0.0, discharge_kw)
        if charge_kw > 0 and discharge_kw > 0:
            # Never charge and discharge in the same interval.
            if charge_kw >= discharge_kw:
                discharge_kw = 0.0
            else:
                charge_kw = 0.0

        if not self.available:
            charge_kw = 0.0
            discharge_kw = 0.0

        dt_h = self.config.interval_hours
        capacity = self.soc_capacity_kwh()

        if charge_kw > 0:
            added_kwh = charge_kw * dt_h * self.config.battery_charge_efficiency
            self.soc_percent = min(100.0, self.soc_percent + 100.0 * added_kwh / capacity)
        if discharge_kw > 0:
            removed_kwh = discharge_kw * dt_h / max(self.config.battery_discharge_efficiency, 1e-6)
            min_soc = self.config.battery_minimum_soc_percent
            self.soc_percent = max(min_soc, self.soc_percent - 100.0 * removed_kwh / capacity)

        heating = 3.0 * (charge_kw + discharge_kw) / max(self.config.battery_max_charge_power_kw, 1e-6)
        self.temperature_c = weather.temperature_c + 2.0 + heating

        status = self._status(charge_kw, discharge_kw)
        return {
            "battery_soc_percent": round(self.soc_percent, 3),
            "battery_soh_percent": round(self.soh_percent, 2),
            "battery_power_kw": round(charge_kw - discharge_kw, 4),
            "battery_charge_power_kw": round(charge_kw, 4),
            "battery_discharge_power_kw": round(discharge_kw, 4),
            "battery_energy_available_kwh": round(self.energy_available_kwh(), 4),
            "battery_temperature_c": round(self.temperature_c, 2),
            "battery_status": status,
            "battery_fault_code": self.fault_code,
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
