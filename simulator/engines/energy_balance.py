"""
Energy dispatch priority and AC-bus balance check.

Priority:
    1. Solar → home
    2. Solar surplus → battery
    3. Remaining solar surplus → grid (clipped by zero-export / volt-watt)
    4. Battery → home (down to the discharge floor the caller passes)
    5. Grid → home
    6. Grid → battery (only when allowed and the battery is not discharging)
    7. Unserved load if grid is down and the battery cannot cover the rest

During an outage the backup port is limited to `backup_output_limit_kw`
(inverter rating). The caller zeroes `solar_kw` for grid-tied homes without
a battery (anti-islanding).

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
    battery_can_charge_from_grid: bool,
    grid_charge_kw: float = 0.0,
    export_limit_kw: float | None = None,
    backup_output_limit_kw: float | None = None,
) -> EnergyFlows:
    remaining_solar = max(0.0, solar_kw)
    remaining_load = max(0.0, load_kw)
    backup_limit = None if grid_available or backup_output_limit_kw is None else max(0.0, backup_output_limit_kw)

    solar_to_home = min(remaining_solar, remaining_load)
    if backup_limit is not None:
        solar_to_home = min(solar_to_home, backup_limit)
    remaining_solar -= solar_to_home
    remaining_load -= solar_to_home

    solar_to_battery = min(remaining_solar, max(0.0, max_charge_kw))
    remaining_solar -= solar_to_battery

    export_cap = remaining_solar
    if zero_export_mode:
        export_cap = min(export_cap, max(0.0, zero_export_limit_kw))
    if export_limit_kw is not None:
        export_cap = min(export_cap, max(0.0, export_limit_kw))
    solar_to_grid = export_cap if grid_available else 0.0
    curtailed = remaining_solar - solar_to_grid

    battery_to_home = min(remaining_load, max(0.0, max_discharge_kw))
    if backup_limit is not None:
        battery_to_home = min(battery_to_home, max(0.0, backup_limit - solar_to_home))
    remaining_load -= battery_to_home

    grid_to_home = remaining_load if grid_available else 0.0
    if grid_available:
        remaining_load = 0.0

    grid_to_battery = 0.0
    if grid_available and battery_can_charge_from_grid and battery_to_home <= 0.0:
        grid_to_battery = min(max(0.0, grid_charge_kw), max(0.0, max_charge_kw - solar_to_battery))
    # Importing and exporting in the same tick is not physical; let grid charging
    # absorb the export first.
    if grid_to_battery > 0.0 and solar_to_grid > 0.0:
        shift = min(grid_to_battery, solar_to_grid)
        solar_to_grid -= shift
        solar_to_battery += shift
        grid_to_battery -= shift

    return EnergyFlows(
        solar_to_home_kw=round(solar_to_home, 4),
        solar_to_battery_kw=round(solar_to_battery, 4),
        solar_to_grid_kw=round(solar_to_grid, 4),
        battery_to_home_kw=round(battery_to_home, 4),
        grid_to_home_kw=round(grid_to_home, 4),
        unserved_load_kw=round(remaining_load, 4),
        solar_curtailed_kw=round(max(0.0, curtailed), 4),
        grid_to_battery_kw=round(grid_to_battery, 4),
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
