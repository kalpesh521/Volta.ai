"""
Map a completed onboarding record onto simulator knobs.

The generator never reads Neon. It fetches this DTO over HTTP
(GET /energy/{household_id}/profile) with the ingest token, then applies
solar capacity, battery, grid, and the selected appliance catalog.
"""
from __future__ import annotations

from typing import Any

from app.modules.onboarding.enums import MeterType, PrimaryGoal, SystemType
from app.modules.onboarding.models import SolarSystem, TrackedAppliance
from app.modules.energy.insights import (
    device_rows_for_profile_text,
    household_profile_text,
    tariff_from_system,
)
from app.modules.energy.schemas import SimulatorDeviceSpec, SimulatorProfileOut

# Typical nameplate Wp used to derive kWp from panel_qty × chemistry.
# Inverter_capacity_kw from onboarding remains the AC clip.
PANEL_WP: dict[str, float] = {
    "Monocrystalline": 400.0,
    "Polycrystalline": 330.0,
    "Thin-film": 300.0,
    "Bifacial": 450.0,
}

# Catalog keys match onboarding ApplianceKey values. Specs match
# simulator/config.DEFAULT_DEVICE_CATALOG plus EV (onboarding has `ev`).
APPLIANCE_DEVICE_SPECS: dict[str, dict[str, Any]] = {
    "fridge": {
        "device_id": "dev_refrigerator",
        "device_name": "Refrigerator",
        "device_type": "refrigerator",
        "rated_power_kw": 0.15,
        "controllable": False,
        "schedule": {"mode": "cyclic", "on_minutes": 25, "cycle_minutes": 45},
    },
    "wash": {
        "device_id": "dev_washing_machine",
        "device_name": "Washing machine",
        "device_type": "washing_machine",
        "rated_power_kw": 0.60,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [{"start": "07:00", "end": "08:00"}],
        },
    },
    "heater": {
        "device_id": "dev_water_heater",
        "device_name": "Water heater",
        "device_type": "water_heater",
        "rated_power_kw": 2.00,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [
                {"start": "06:00", "end": "07:00"},
                {"start": "18:00", "end": "19:00"},
            ],
        },
    },
    "ac": {
        "device_id": "dev_air_conditioner",
        "device_name": "Air conditioner",
        "device_type": "air_conditioner",
        "rated_power_kw": 1.50,
        "controllable": True,
        "schedule": {
            "mode": "temperature_or_windows",
            "on_above_c": 29.0,
            "windows": [
                {"start": "13:00", "end": "16:00"},
                {"start": "21:00", "end": "23:00"},
            ],
        },
    },
    "pump": {
        "device_id": "dev_water_pump",
        "device_name": "Water pump",
        "device_type": "water_pump",
        "rated_power_kw": 0.75,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [{"start": "06:30", "end": "07:00"}],
        },
    },
    "ev": {
        "device_id": "dev_ev_charger",
        "device_name": "EV charger",
        "device_type": "ev_charger",
        "rated_power_kw": 3.30,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [{"start": "22:00", "end": "06:00"}],
        },
    },
}


def solar_capacity_kwp(panel_type: str, panel_qty: int) -> float:
    wp = PANEL_WP.get(panel_type, 400.0)
    return round(max(0, panel_qty) * wp / 1000.0, 3)


def _devices(appliances: list[TrackedAppliance]) -> list[SimulatorDeviceSpec]:
    out: list[SimulatorDeviceSpec] = []
    for row in appliances:
        spec = APPLIANCE_DEVICE_SPECS.get(row.appliance_key)
        if spec is None:
            continue
        payload = dict(spec)
        payload["critical"] = bool(row.is_critical)
        out.append(SimulatorDeviceSpec.model_validate(payload))
    return out


def build_simulator_profile(system: SolarSystem) -> SimulatorProfileOut:
    inverter_kw = float(system.inverter_capacity_kw)
    battery = system.battery_config
    battery_present = (
        system.system_type != SystemType.ON_GRID.value and battery is not None
    )
    capacity = float(battery.battery_capacity_kwh) if battery_present and battery else 0.0
    reserve = float(battery.reserve_pct) if battery_present and battery else 20.0
    usable = round(capacity * max(0.0, (100.0 - reserve) / 100.0), 3) if capacity else 0.0

    grid_available = system.system_type != SystemType.OFF_GRID.value
    meter = system.grid_config.meter_type if system.grid_config else None
    zero_export = bool(grid_available and meter == MeterType.NO_EXPORT.value)
    tariff = tariff_from_system(system)
    devices = _devices(list(system.appliances))
    goal = system.primary_goal or PrimaryGoal.MAXIMIZE_SELF_CONSUMPTION.value
    kwp = solar_capacity_kwp(system.panel_type, system.panel_qty)
    min_soc = reserve if battery_present else 20.0

    return SimulatorProfileOut(
        household_id=system.household_id,
        system_type=system.system_type,
        panel_type=system.panel_type,
        panel_qty=system.panel_qty,
        solar_capacity_kwp=kwp,
        inverter_capacity_kw=inverter_kw,
        battery_present=battery_present,
        battery_capacity_kwh=capacity,
        battery_usable_capacity_kwh=usable,
        battery_minimum_soc_percent=min_soc,
        battery_max_charge_power_kw=inverter_kw if battery_present else 0.0,
        battery_max_discharge_power_kw=inverter_kw if battery_present else 0.0,
        grid_available=grid_available,
        zero_export_mode=zero_export,
        location=system.location,
        primary_goal=goal,
        tariff_type=tariff.tariff_type,
        tariff_rate=tariff.tariff_rate,
        export_credit_inr_per_kwh=tariff.export_credit_inr_per_kwh,
        meter_type=tariff.meter_type,
        devices=devices,
        text=household_profile_text(
            household_id=system.household_id,
            system_type=system.system_type,
            location=system.location,
            primary_goal=goal,
            solar_capacity_kwp=kwp,
            battery_present=battery_present,
            battery_capacity_kwh=capacity,
            battery_minimum_soc_percent=min_soc,
            tariff_type=tariff.tariff_type,
            tariff_rate=tariff.tariff_rate,
            export_credit_inr_per_kwh=tariff.export_credit_inr_per_kwh,
            devices=device_rows_for_profile_text(devices),
        ),
    )
