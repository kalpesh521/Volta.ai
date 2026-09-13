"""
Derived fields for energy APIs and the AI assistant context pack.

Peak load, device priority, and estimated savings are computed here so
producers cannot self-certify them. Narrative `text` is deterministic from
structured fields — it is not stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable

from app.core.config import settings
from app.modules.onboarding.enums import SystemType
from app.modules.onboarding.models import SolarSystem

SAVINGS_CURRENCY = "INR"
SAVINGS_METHOD = "self_consumed_solar_plus_export_credit"


class DevicePriority(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    FLEXIBLE = "flexible"


@dataclass(frozen=True)
class HouseholdTariff:
    """Import ₹/kWh (`tariff_rate`) and export credit ₹/kWh from grid config."""

    tariff_type: str | None
    tariff_rate: float | None
    export_credit_inr_per_kwh: float | None
    meter_type: str | None
    applicable: bool

    @property
    def savings_method(self) -> str | None:
        return SAVINGS_METHOD if self.applicable else None


UNAVAILABLE_TARIFF = HouseholdTariff(None, None, None, None, False)


def important_load_kw() -> float:
    return float(settings.DEVICE_IMPORTANT_POWER_KW)


def derive_device_priority(*, critical: bool, rated_power_kw: float) -> str:
    """
    Rank a device for load-shedding / assistant advice.

    critical=true always wins (fridge, medical, user lock).
    Otherwise nameplate ≥ DEVICE_IMPORTANT_POWER_KW is important; the rest is flexible.
    """
    if critical:
        return DevicePriority.CRITICAL.value
    if rated_power_kw + 1e-12 >= important_load_kw():
        return DevicePriority.IMPORTANT.value
    return DevicePriority.FLEXIBLE.value


def peak_load_kw(load_kw_values: Iterable[float]) -> float:
    values = list(load_kw_values)
    if not values:
        return 0.0
    return round(max(values), 6)


def _money(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def tariff_from_system(system: SolarSystem | None) -> HouseholdTariff:
    """Off-grid (or missing grid row) has no rupee tariff."""
    if system is None:
        return UNAVAILABLE_TARIFF
    if system.system_type == SystemType.OFF_GRID.value:
        return UNAVAILABLE_TARIFF
    grid = system.grid_config
    if grid is None:
        return UNAVAILABLE_TARIFF
    rate = _money(grid.energy_charge_inr_per_kwh)
    credit = _money(grid.export_credit_inr_per_kwh)
    if rate is None or credit is None:
        return UNAVAILABLE_TARIFF
    return HouseholdTariff(
        tariff_type=grid.tariff_type,
        tariff_rate=rate,
        export_credit_inr_per_kwh=credit,
        meter_type=grid.meter_type,
        applicable=True,
    )


def estimated_savings_inr(
    *,
    solar_generation_kwh: float,
    grid_export_kwh: float,
    tariff: HouseholdTariff,
) -> float | None:
    """
    Value of solar vs buying the same kWh from the grid:

      self_consumed_kwh = max(0, solar − export)
      savings = self_consumed × import_rate + export × export_credit

    Not a DISCOM bill. Null when the home has no grid tariff.
    """
    if (
        not tariff.applicable
        or tariff.tariff_rate is None
        or tariff.export_credit_inr_per_kwh is None
    ):
        return None
    export = max(0.0, grid_export_kwh)
    self_consumed = max(0.0, solar_generation_kwh - export)
    savings = (
        self_consumed * tariff.tariff_rate
        + export * tariff.export_credit_inr_per_kwh
    )
    return round(savings, 2)


def _goal_label(primary_goal: str) -> str:
    return primary_goal.replace("_", " ")


def live_snapshot_text(
    *,
    timestamp: datetime,
    solar_kw: float,
    load_kw: float,
    battery_soc_percent: float,
    battery_status: str,
    grid_status: str,
    import_kw: float,
    export_kw: float,
    energy_balance_status: str,
    data_quality: str,
) -> str:
    stamp = timestamp.isoformat(timespec="seconds")
    if export_kw > 0.02:
        grid_flow = f"exporting {export_kw:.2f} kW"
    elif import_kw > 0.02:
        grid_flow = f"importing {import_kw:.2f} kW"
    else:
        grid_flow = "idle"
    return (
        f"At {stamp}: solar {solar_kw:.2f} kW, load {load_kw:.2f} kW, "
        f"battery {battery_soc_percent:.0f}% ({battery_status}), "
        f"grid {grid_status} ({grid_flow}). "
        f"Energy balance {energy_balance_status}. Data quality {data_quality}."
    )


def daily_summary_text(
    *,
    day: date,
    timezone_name: str,
    reading_count: int,
    solar_generation_kwh: float,
    home_consumption_kwh: float,
    grid_import_kwh: float,
    grid_export_kwh: float,
    battery_charge_kwh: float,
    battery_discharge_kwh: float,
    peak_load: float,
    estimated_savings: float | None,
    tariff: HouseholdTariff,
) -> str:
    if reading_count == 0:
        return (
            f"No telemetry readings for {day.isoformat()} ({timezone_name}). "
            "Daily kWh totals are zero."
        )
    if estimated_savings is None or not tariff.applicable:
        savings_clause = (
            "Estimated savings unavailable — this home has no numeric tariff rates."
        )
    else:
        savings_clause = (
            f"Estimated savings ₹{estimated_savings:.2f} "
            f"(self-consumed solar at ₹{tariff.tariff_rate:.2f}/kWh "
            f"plus export credit ₹{tariff.export_credit_inr_per_kwh:.2f}/kWh)."
        )
    return (
        f"Daily energy for {day.isoformat()} ({timezone_name}): "
        f"generated {solar_generation_kwh:.3f} kWh, home used {home_consumption_kwh:.3f} kWh, "
        f"imported {grid_import_kwh:.3f} kWh, exported {grid_export_kwh:.3f} kWh, "
        f"battery charge {battery_charge_kwh:.3f} kWh / discharge {battery_discharge_kwh:.3f} kWh. "
        f"Peak load {peak_load:.2f} kW across {reading_count} readings. "
        f"{savings_clause}"
    )


def household_profile_text(
    *,
    household_id: str,
    system_type: str,
    location: str | None,
    primary_goal: str,
    solar_capacity_kwp: float,
    battery_present: bool,
    battery_capacity_kwh: float,
    battery_minimum_soc_percent: float,
    tariff_type: str | None,
    tariff_rate: float | None,
    export_credit_inr_per_kwh: float | None,
    devices: list[tuple[str, str, float]],
) -> str:
    """devices: (name, priority, rated_power_kw)."""
    place = location.strip() if location and location.strip() else "unspecified location"
    if battery_present and battery_capacity_kwh > 0:
        battery_clause = (
            f"{battery_capacity_kwh:.1f} kWh battery (min SOC {battery_minimum_soc_percent:.0f}%)"
        )
    else:
        battery_clause = "no battery"
    if tariff_rate is not None:
        credit = export_credit_inr_per_kwh if export_credit_inr_per_kwh is not None else 0.0
        kind = tariff_type or "tariff"
        tariff_clause = (
            f"Tariff: {kind} at ₹{tariff_rate:.2f}/kWh import, "
            f"₹{credit:.2f}/kWh export credit."
        )
    else:
        tariff_clause = "Tariff: no numeric rate on file."
    if devices:
        listed = "; ".join(
            f"{name} ({priority}, {power:.2f} kW)" for name, priority, power in devices
        )
        device_clause = f"Devices: {listed}."
    else:
        device_clause = "Devices: none tracked."
    return (
        f"{system_type} home {household_id} in {place}. "
        f"Goal: {_goal_label(primary_goal)}. "
        f"{solar_capacity_kwp:.2f} kWp solar, {battery_clause}. "
        f"{tariff_clause} {device_clause}"
    )


def combined_text(*parts: str | None) -> str:
    chunks = [part.strip() for part in parts if part and part.strip()]
    return "\n".join(chunks)


def device_rows_for_profile_text(devices: list[Any]) -> list[tuple[str, str, float]]:
    rows: list[tuple[str, str, float]] = []
    for spec in devices:
        name = getattr(spec, "device_name", None) or getattr(spec, "device_type", "device")
        priority = getattr(spec, "device_priority", "flexible")
        power = float(getattr(spec, "rated_power_kw", 0.0) or 0.0)
        rows.append((str(name), str(priority), power))
    return rows
