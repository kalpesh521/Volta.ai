"""
Compose weather + solar + load + battery + grid into one telemetry record.

This is the only module that knows the full reading. Individual engines stay
replaceable so new fields can be added in one place (`TelemetryRecord` + here).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from simulator.engines.battery import BatteryEngine
from simulator.config import SimulatorConfig, apply_scenario
from simulator.engines.device import DeviceEngine
from simulator.engines.energy_balance import dispatch_energy, validate_energy_balance
from simulator.clients.geocoding import apply_location
from simulator.engines.grid import GridEngine, grid_is_available
from simulator.dashboard.live_bus import bus
from simulator.engines.load import LoadGenerator
from simulator.dashboard import location_state
from simulator.models import LocationRecord, TelemetryRecord, WeatherRecord
from simulator.engines.solar import SolarGenerator
from simulator.clients.weather import WeatherClient


class TelemetryGenerator:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = apply_scenario(config, config.scenario)
        self.weather_client = WeatherClient(self.config)
        self.solar = SolarGenerator(self.config)
        self.devices = DeviceEngine(self.config)
        self.load = LoadGenerator(self.config, self.devices)
        self.battery = BatteryEngine(self.config)
        self.grid = GridEngine(self.config)
        self._writer: TextIO | None = None
        self._output_path: Path | None = None

    async def warmup(self, ts: datetime) -> None:
        await self.weather_client.warmup(ts)

    async def apply_location(self, location: LocationRecord, ts: datetime) -> None:
        """Switch home coordinates/timezone and refresh weather for that place."""
        self.config = apply_location(self.config, location)
        location_state.current_location = location
        self.solar.config = self.config
        self.devices.config = self.config
        self.load.config = self.config
        self.battery.config = self.config
        self.grid.config = self.config
        self.weather_client = WeatherClient(self.config)
        await self.weather_client.warmup(ts)

    def open_output(self, path: Path | None = None) -> Path:
        output = path or self.config.output_path()
        output.parent.mkdir(parents=True, exist_ok=True)
        self._writer = output.open("a", encoding="utf-8")
        self._output_path = output
        return output

    def close_output(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    def write_record(self, record: TelemetryRecord) -> None:
        if self._writer is None:
            return
        self._writer.write(record.model_dump_json() + "\n")
        self._writer.flush()

    async def step(self, ts: datetime) -> TelemetryRecord:
        tz = ZoneInfo(self.config.timezone)
        local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)

        weather: WeatherRecord = await self.weather_client.get_weather(local)
        potential_solar = self.solar.compute_power_kw(local, weather)
        demand_kw, device_readings = self.load.generate(local, weather)
        grid_up = grid_is_available(self.config, local)

        flows = dispatch_energy(
            solar_kw=potential_solar,
            load_kw=demand_kw,
            max_charge_kw=self.battery.max_charge_kw(),
            max_discharge_kw=self.battery.max_discharge_kw(),
            grid_available=grid_up,
            zero_export_mode=self.config.zero_export_mode,
            zero_export_limit_kw=self.config.zero_export_limit_kw,
            battery_can_charge_from_grid=self.config.battery_can_charge_from_grid,
        )

        actual_solar = (
            flows.solar_to_home_kw
            + flows.solar_to_battery_kw
            + flows.solar_to_grid_kw
        )
        served_kw = (
            flows.solar_to_home_kw
            + flows.battery_to_home_kw
            + flows.grid_to_home_kw
        )

        solar_fields = self.solar.accumulate(local, actual_solar)
        load_fields = self.load.record_served(served_kw, local)
        battery_fields = self.battery.apply(
            flows.solar_to_battery_kw,
            flows.battery_to_home_kw,
            weather,
        )
        grid_fields = self.grid.apply(
            local,
            flows.grid_to_home_kw,
            flows.solar_to_grid_kw,
            grid_up,
        )

        balance = validate_energy_balance(
            solar_kw=actual_solar,
            grid_import_kw=float(grid_fields["grid_import_power_kw"]),
            battery_discharge_kw=float(battery_fields["battery_discharge_power_kw"]),
            home_consumption_kw=served_kw,
            grid_export_kw=float(grid_fields["grid_export_power_kw"]),
            battery_charge_kw=float(battery_fields["battery_charge_power_kw"]),
            charge_efficiency=self.config.battery_charge_efficiency,
            discharge_efficiency=self.config.battery_discharge_efficiency,
            tolerance_kw=self.config.energy_balance_tolerance_kw,
        )

        data_quality = weather.data_quality
        if self.config.scenario == "sensor_failure":
            data_quality = "degraded"
        elif data_quality not in {"fallback", "degraded"}:
            data_quality = "simulated"

        extra: dict[str, Any] = {
            "schema_version": self.config.schema_version,
            "simulation_time": local.isoformat(),
            "wall_clock_time": datetime.now(tz=tz).isoformat(),
            "solar_potential_kw": round(potential_solar, 4),
            "solar_curtailed_kw": flows.solar_curtailed_kw,
            "battery_power_kw": battery_fields["battery_power_kw"],
            "battery_temperature_c": battery_fields["battery_temperature_c"],
            "battery_fault_code": battery_fields["battery_fault_code"],
            "grid_import_interval_kwh": grid_fields["grid_import_interval_kwh"],
            "grid_export_interval_kwh": grid_fields["grid_export_interval_kwh"],
            "grid_voltage_v": grid_fields["grid_voltage_v"],
            "grid_frequency_hz": grid_fields["grid_frequency_hz"],
            "system_losses_kw": balance.system_losses_kw,
            "warnings": list(balance.warnings),
            "scenario": self.config.scenario,
            "location": location_state.current_location.model_dump(mode="json"),
        }

        record = TelemetryRecord(
            timestamp=local,
            household_id=self.config.household_id,
            data_source="simulator",
            data_quality=data_quality,
            solar_power_kw=solar_fields["solar_power_kw"],
            solar_energy_interval_kwh=solar_fields["solar_energy_interval_kwh"],
            solar_energy_today_kwh=solar_fields["solar_energy_today_kwh"],
            solar_energy_total_kwh=solar_fields["solar_energy_total_kwh"],
            home_load_power_kw=demand_kw,
            home_consumption_interval_kwh=load_fields["home_consumption_interval_kwh"],
            home_consumption_today_kwh=load_fields["home_consumption_today_kwh"],
            home_consumption_total_kwh=load_fields["home_consumption_total_kwh"],
            battery_soc_percent=float(battery_fields["battery_soc_percent"]),
            battery_soh_percent=float(battery_fields["battery_soh_percent"]),
            battery_charge_power_kw=float(battery_fields["battery_charge_power_kw"]),
            battery_discharge_power_kw=float(battery_fields["battery_discharge_power_kw"]),
            battery_energy_available_kwh=float(battery_fields["battery_energy_available_kwh"]),
            battery_status=str(battery_fields["battery_status"]),
            grid_status=str(grid_fields["grid_status"]),
            grid_import_power_kw=float(grid_fields["grid_import_power_kw"]),
            grid_export_power_kw=float(grid_fields["grid_export_power_kw"]),
            total_import_kwh=float(grid_fields["total_import_kwh"]),
            total_export_kwh=float(grid_fields["total_export_kwh"]),
            solar_to_home_kw=flows.solar_to_home_kw,
            solar_to_battery_kw=flows.solar_to_battery_kw,
            solar_to_grid_kw=flows.solar_to_grid_kw,
            battery_to_home_kw=flows.battery_to_home_kw,
            grid_to_home_kw=flows.grid_to_home_kw,
            unserved_load_kw=flows.unserved_load_kw,
            energy_balance_status=balance.energy_balance_status,
            energy_balance_error_kw=balance.energy_balance_error_kw,
            energy_balance_valid=balance.energy_balance_valid,
            devices=[item.model_dump(mode="json") for item in device_readings],
            weather=weather.model_dump(mode="json"),
            **extra,
        )
        self.write_record(record)
        bus.publish(record.model_dump(mode="json"))
        return record
