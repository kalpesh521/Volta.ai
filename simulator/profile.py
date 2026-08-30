"""
Apply a backend SimulatorProfileOut JSON object onto SimulatorConfig.

The backend owns the mapping (panel qty → kWp, appliances → catalog).
This module only copies already-validated knobs onto the generator config.
"""

from __future__ import annotations

import json
from typing import Any

from simulator.config import SimulatorConfig


def apply_onboarding_profile(
    config: SimulatorConfig,
    profile: dict[str, Any],
) -> SimulatorConfig:
    """
    Overlay onboarding-derived knobs. Does not change interval, weather mode,
    scenario, or ingest/RabbitMQ URLs — those stay CLI/env.
    """
    devices = profile.get("devices") or []
    if not isinstance(devices, list):
        raise ValueError("profile.devices must be a JSON array")

    updates: dict[str, Any] = {
        "household_id": str(profile["household_id"]),
        "solar_capacity_kwp": float(profile["solar_capacity_kwp"]),
        "inverter_capacity_kw": float(profile["inverter_capacity_kw"]),
        "battery_present": bool(profile["battery_present"]),
        "battery_capacity_kwh": float(profile["battery_capacity_kwh"]),
        "battery_usable_capacity_kwh": float(profile["battery_usable_capacity_kwh"]),
        "battery_minimum_soc_percent": float(profile["battery_minimum_soc_percent"]),
        "battery_max_charge_power_kw": float(profile["battery_max_charge_power_kw"]),
        "battery_max_discharge_power_kw": float(profile["battery_max_discharge_power_kw"]),
        "grid_available": bool(profile["grid_available"]),
        "zero_export_mode": bool(profile["zero_export_mode"]),
        "device_catalog_json": json.dumps(devices),
    }
    location = profile.get("location")
    if isinstance(location, str) and location.strip():
        updates["location_name"] = location.strip()
        updates["geocode_on_start"] = True

    if not updates["battery_present"]:
        updates["battery_capacity_kwh"] = 0.0
        updates["battery_usable_capacity_kwh"] = 0.0
        updates["battery_max_charge_power_kw"] = 0.0
        updates["battery_max_discharge_power_kw"] = 0.0

    return config.model_copy(update=updates)
