"""
Energy dispatch priority and AC-bus balance check.

Priority:
    1. Solar → home
    2. Solar surplus → battery
    3. Remaining solar surplus → grid (clipped in zero-export mode)
    4. Battery → home
    5. Grid → home
    6. Unserved load if grid is down and the battery cannot cover the rest

Validation never stops the generator; it only sets status / warning fields.
"""

from __future__ import annotations

from simulator.models import EnergyBalanceResult, EnergyFlows


def dispatch_energy(
    *,
    solar_kw: float,
    load_kw: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    grid_available: bool,
    zero_export_mode: bool,
    zero_export_limit_kw: float,
    battery_can_charge_from_grid: bool,  # reserved; grid→battery charge is off
) -> EnergyFlows:
    remaining_solar = max(0.0, solar_kw)
    remaining_load = max(0.0, load_kw)

    solar_to_home = min(remaining_solar, remaining_load)
    remaining_solar -= solar_to_home
    remaining_load -= solar_to_home

    solar_to_battery = min(remaining_solar, max(0.0, max_charge_kw))
    remaining_solar -= solar_to_battery

    export_cap = 0.0 if zero_export_mode else remaining_solar
    if zero_export_mode:
        export_cap = min(remaining_solar, max(0.0, zero_export_limit_kw))
    solar_to_grid = export_cap if grid_available else 0.0
    curtailed = remaining_solar - solar_to_grid

    battery_to_home = min(remaining_load, max(0.0, max_discharge_kw))
    remaining_load -= battery_to_home

    grid_to_home = remaining_load if grid_available else 0.0
    if grid_available:
        remaining_load = 0.0

    return EnergyFlows(
        solar_to_home_kw=round(solar_to_home, 4),
        solar_to_battery_kw=round(solar_to_battery, 4),
        solar_to_grid_kw=round(solar_to_grid, 4),
        battery_to_home_kw=round(battery_to_home, 4),
        grid_to_home_kw=round(grid_to_home, 4),
        unserved_load_kw=round(remaining_load, 4),
        solar_curtailed_kw=round(max(0.0, curtailed), 4),
        grid_to_battery_kw=0.0,
    )


def validate_energy_balance(
    *,
    solar_kw: float,
    grid_import_kw: float,
    battery_discharge_kw: float,
    home_consumption_kw: float,
    grid_export_kw: float,
    battery_charge_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    tolerance_kw: float,
) -> EnergyBalanceResult:
    """
    AC-bus check on interval power (kW only — never mix in kWh totals):

        solar + grid_import + battery_discharge
            ≈ home + grid_export + battery_charge

    Every operand is power for the same tick. kWh is derived later as
    power_kw * interval_seconds / 3600. Conversion losses are estimated
    separately and absorbed by `tolerance_kw`.
    """
    inputs = solar_kw + grid_import_kw + battery_discharge_kw
    outputs = home_consumption_kw + grid_export_kw + battery_charge_kw
    error = inputs - outputs

    charge_loss = battery_charge_kw * (1.0 - charge_efficiency)
    discharge_loss = battery_discharge_kw * (
        (1.0 / max(discharge_efficiency, 1e-6)) - 1.0
    )
    system_losses = charge_loss + discharge_loss

    warnings: list[str] = []
    valid = abs(error) <= tolerance_kw
    if not valid:
        warnings.append(
            f"energy_balance_error_kw={error:.4f} exceeds tolerance={tolerance_kw:.4f}"
        )

    return EnergyBalanceResult(
        energy_balance_status="valid" if valid else "warning",
        energy_balance_error_kw=round(error, 6),
        energy_balance_valid=valid,
        system_losses_kw=round(system_losses, 6),
        warnings=warnings,
    )
