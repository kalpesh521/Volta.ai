"""
Apply a backend SimulatorProfileOut JSON object onto SimulatorConfig.

The backend owns the mapping (panel qty → kWp, appliances → catalog).
This module only copies already-validated knobs onto the generator config.
"""

from __future__ import annotations

import json
from typing import Any

from simulator.config import SimulatorConfig

# Pmax temperature coefficient (per °C) by panel technology keyword.
_PANEL_TEMP_COEFFICIENTS = (
    ("thin", 0.0025),
    ("topcon", 0.0030),
    ("hjt", 0.0026),
    ("hetero", 0.0026),
    ("bifacial", 0.0034),
    ("mono", 0.0035),
    ("poly", 0.0040),
)


def _temp_coefficient(panel_type: str | None) -> float | None:
    text = (panel_type or "").lower()
    for keyword, coefficient in _PANEL_TEMP_COEFFICIENTS:
        if keyword in text:
            return coefficient
    return None


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
        "system_type": str(profile.get("system_type") or config.system_type),
        "primary_goal": str(profile.get("primary_goal") or "maximize_self_consumption"),
        "tariff_type": profile.get("tariff_type"),
        "tariff_rate_inr_per_kwh": profile.get("tariff_rate"),
        "export_credit_inr_per_kwh": profile.get("export_credit_inr_per_kwh"),
        "meter_type": profile.get("meter_type"),
    }
    coefficient = _temp_coefficient(profile.get("panel_type"))
    if coefficient is not None:
        updates["pv_temp_coefficient_per_c"] = coefficient
    # Hybrid inverters can charge from the grid; the goal policy decides when.
    updates["battery_can_charge_from_grid"] = bool(
        updates["battery_present"] and "hybrid" in updates["system_type"].lower()
    )
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
