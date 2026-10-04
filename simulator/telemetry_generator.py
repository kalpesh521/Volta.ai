"""
Compose weather + solar + load + battery + grid into one telemetry record.

This is the only module that knows the full reading. Individual engines stay
replaceable so new fields can be added in one place (`TelemetryRecord` + here).

Per tick:
    1. Pick the day's scenario (fixed, or `auto` = season-aware daily mix).
    2. Weather (live / synthetic climate) → solar snapshot, loads, grid state.
    3. Energy-management policy: primary goal, discharge floor, grid charging,
       volt-watt export cap, anti-islanding and outage load shedding.
    4. Dispatch → battery / grid engines → balance check.
    5. Events (diff against the previous tick), ground-truth condition
       labels and daily KPIs for downstream AI.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from simulator.clients.geocoding import GeocodingClient, apply_location
from simulator.clients.weather import WeatherClient
from simulator.config import SimulatorConfig, apply_scenario
from simulator.dashboard import location_state
from simulator.dashboard.live_bus import bus
from simulator.engines.battery import BatteryEngine
from simulator.engines.device import DeviceEngine
from simulator.engines.energy_balance import dispatch_energy, validate_energy_balance
from simulator.engines.grid import (
    GridEngine,
    export_limit_kw,
    feeder_voltage_v,
    grid_outage_cause,
    tariff_period,
)
from simulator.engines.load import LoadGenerator
from simulator.engines.solar import SolarGenerator
from simulator.models import DeviceReading, LocationRecord, TelemetryRecord, WeatherRecord
from simulator.scenarios import SCENARIO_INFO, STARTUP_ONLY, scenario_for_day

logger = logging.getLogger("suryaa.generator")

STATE_VERSION = 1
_NOISY_DEVICE_TYPES = {"refrigerator", "fridge"}


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator <= 1e-6:
        return None
    return round(max(0.0, min(100.0, 100.0 * numerator / denominator)), 2)


class TelemetryGenerator:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = apply_scenario(config, config.scenario)
        self.weather_client = WeatherClient(self.config)
        self.solar = SolarGenerator(self.config)
        self.devices = DeviceEngine(self.config)
        self.load = LoadGenerator(self.config, self.devices)
        self.battery = BatteryEngine(self.config)
        self.grid = GridEngine(self.config)
        self.home_location = GeocodingClient(self.config).from_config()
        self._writer: TextIO | None = None
        self._output_path: Path | None = None
        self._prev: dict[str, Any] | None = None
        self._daily: dict[str, Any] = self._empty_daily(None)

    # ---- wiring ------------------------------------------------------------------

    async def warmup(self, ts: datetime) -> None:
        await self.weather_client.warmup(ts)

    def _set_config(self, config: SimulatorConfig) -> None:
        self.config = config
        self.solar.config = config
        self.devices.config = config
        self.load.config = config
        self.battery.config = config
        self.grid.config = config

    async def apply_location(self, location: LocationRecord, ts: datetime) -> None:
        """Switch home coordinates/timezone and refresh weather for that place."""
        self._set_config(apply_location(self.config, location))
        location_state.current_location = location
        self.weather_client = WeatherClient(self.config)
        self.home_location = location
        self._prev = None
        await self.weather_client.warmup(ts)

    # ---- output ------------------------------------------------------------------

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

    def _rotate_if_needed(self) -> None:
        if self._writer is None or self._output_path is None:
            return
        limit = self.config.output_max_mb * 1024 * 1024
        if limit <= 0 or self._writer.tell() < limit:
            return
        self._writer.close()
        path = self._output_path
        backups = max(0, self.config.output_backups)
        if backups == 0:
            path.unlink(missing_ok=True)
        else:
            oldest = path.with_name(f"{path.name}.{backups}")
            oldest.unlink(missing_ok=True)
            for i in range(backups - 1, 0, -1):
                src = path.with_name(f"{path.name}.{i}")
                if src.exists():
                    src.replace(path.with_name(f"{path.name}.{i + 1}"))
            path.replace(path.with_name(f"{path.name}.1"))
        self._writer = path.open("a", encoding="utf-8")
        logger.info("rotated telemetry output %s", path)

    def write_record(self, record: TelemetryRecord) -> None:
        if self._writer is None:
            return
        self._writer.write(record.model_dump_json() + "\n")
        self._writer.flush()
        self._rotate_if_needed()

    # ---- persistence -------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "household_id": self.config.household_id,
            "saved_at": datetime.now().astimezone().isoformat(),
            "solar": self.solar.state(),
            "load": self.load.state(),
            "battery": self.battery.state(),
            "grid": self.grid.state(),
            "daily": {**self._daily, "day": self._daily["day"].isoformat() if self._daily["day"] else None},
        }

    def restore(self, data: dict[str, Any]) -> None:
        if data.get("version") != STATE_VERSION or data.get("household_id") != self.config.household_id:
            return
        self.solar.restore(data.get("solar") or {})
        self.load.restore(data.get("load") or {})
        self.battery.restore(data.get("battery") or {}, keep_soc=self.config.scenario in STARTUP_ONLY)
        self.grid.restore(data.get("grid") or {})
        daily = dict(data.get("daily") or {})
        if daily.get("day"):
            daily["day"] = date.fromisoformat(daily["day"])
            self._daily = {**self._empty_daily(daily["day"]), **daily}

    def save_state(self, path: Path | None = None) -> Path:
        target = path or self.config.state_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state(), default=str), encoding="utf-8")
        os.replace(tmp, target)
        return target

    def load_state(self, path: Path | None = None) -> bool:
        source = path or self.config.state_path()
        if not source.exists():
            return False
        try:
            self.restore(json.loads(source.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("ignoring unreadable simulator state %s: %s", source, exc)
            return False
        return True

    # ---- daily KPIs --------------------------------------------------------------

    @staticmethod
    def _empty_daily(day: date | None) -> dict[str, Any]:
        return {
            "day": day,
            "grid_to_home_kwh": 0.0,
            "unserved_kwh": 0.0,
            "curtailed_kwh": 0.0,
            "demand_kwh": 0.0,
        }

    def _update_daily(self, local: datetime, flows: Any, demand_kw: float) -> None:
        if self._daily["day"] != local.date():
            self._daily = self._empty_daily(local.date())
        dt_h = self.config.interval_hours
        self._daily["grid_to_home_kwh"] += flows.grid_to_home_kw * dt_h
        self._daily["unserved_kwh"] += flows.unserved_load_kw * dt_h
        self._daily["curtailed_kwh"] += flows.solar_curtailed_kw * dt_h
        self._daily["demand_kwh"] += demand_kw * dt_h

    # ---- policy ------------------------------------------------------------------

    def scenario_for(self, local: datetime) -> str:
        cfg = self.config
        return scenario_for_day(
            cfg.scenario,
            local.date(),
            latitude=cfg.latitude,
            seed=cfg.household_seed,
            synthetic_weather=cfg.synthetic_weather,
        )

    def _battery_policy(self, period: str, grid_up: bool) -> tuple[float, float]:
        """(discharge floor SOC %, requested grid→battery kW)."""
        cfg = self.config
        floor = cfg.battery_minimum_soc_percent
        if not grid_up or not self.battery.available:
            return floor, 0.0
        soc = self.battery.soc_percent
        goal = cfg.primary_goal
        reserve = max(floor, cfg.battery_backup_reserve_percent)
        grid_charge = 0.0
        if goal == "preserve_backup":
            floor = reserve
            if cfg.battery_can_charge_from_grid and soc < reserve - 1.0:
                grid_charge = 0.5 * cfg.battery_max_charge_power_kw
        elif goal == "minimize_bill" and period == "off_peak":
            # Hold stored energy for the expensive peak; top up cheaply.
            floor = max(floor, soc)
            if cfg.battery_can_charge_from_grid and soc < cfg.grid_charge_target_soc_percent:
                grid_charge = 0.5 * cfg.battery_max_charge_power_kw
        return floor, grid_charge

    def _shed_for_outage(
        self, demand_kw: float, devices: list[DeviceReading], supply_kw: float
    ) -> tuple[float, list[DeviceReading], list[str]]:
        """Drop appliances the backup cannot carry (largest non-critical first)."""
        if demand_kw <= supply_kw + 1e-6:
            return demand_kw, devices, []
        if supply_kw <= 0.01:
            ids = {d.device_id for d in devices if d.current_power_kw > 0}
            mode = "power_cut"
        else:
            ids = set()
            excess = demand_kw - supply_kw
            for reading in sorted(devices, key=lambda d: d.current_power_kw, reverse=True):
                if excess <= 0:
                    break
                if reading.critical or reading.current_power_kw <= 0:
                    continue
                ids.add(reading.device_id)
                excess -= reading.current_power_kw
            mode = "load_shed"
        if not ids:
            return demand_kw, devices, []
        removed = sum(d.current_power_kw for d in devices if d.device_id in ids)
        devices = self.devices.shed(devices, ids, mode)
        demand_kw = round(max(0.0, demand_kw - removed), 4)
        if self.load.last_breakdown:
            self.load.last_breakdown["tracked_devices"] = round(sum(d.current_power_kw for d in devices), 4)
        return demand_kw, devices, sorted(ids)

    # ---- events / labels -----------------------------------------------------------

    def _events(self, now: dict[str, Any]) -> list[dict[str, Any]]:
        prev = self._prev
        if prev is None:
            return []
        events: list[dict[str, Any]] = []

        def add(kind: str, severity: str, message: str, **data: Any) -> None:
            events.append({"type": kind, "severity": severity, "message": message, **data})

        if prev["scenario"] != now["scenario"]:
            add("scenario_changed", "info", f"Scenario for today: {now['scenario']}", scenario=now["scenario"])
        if prev["grid_up"] and not now["grid_up"]:
            add("grid_outage_started", "critical", f"Grid power lost ({now['outage_cause']})", cause=now["outage_cause"])
        elif not prev["grid_up"] and now["grid_up"]:
            add("grid_restored", "info", "Grid power restored", outage_minutes=now["outage_minutes"])
        if prev["soc"] < 99.5 <= now["soc"]:
            add("battery_full", "info", "Battery fully charged")
        if prev["soc"] > now["floor"] + 0.5 >= now["soc"] and now["battery_present"]:
            add("battery_reserve_reached", "warning", f"Battery reached its {now['floor']:.0f}% floor")
        if not prev["fault"] and now["fault"]:
            add("battery_fault", "critical", f"Battery fault code {now['fault']}", fault_code=now["fault"])
        elif prev["fault"] and not now["fault"]:
            add("battery_fault_cleared", "info", "Battery fault cleared")
        if prev["derate"] != now["derate"] and now["derate"] in ("high_temperature", "low_temperature"):
            add("battery_derated", "warning", f"Battery power derated ({now['derate']})", reason=now["derate"])
        if not prev["producing"] and now["producing"]:
            add("solar_production_started", "info", "Solar production started")
        elif prev["producing"] and not now["producing"]:
            add("solar_production_ended", "info", "Solar production ended")
        if prev["inverter"] != "clipping" and now["inverter"] == "clipping":
            add("inverter_clipping", "info", "Inverter at its AC limit; extra PV is clipped")
        if not prev["volt_watt"] and now["volt_watt"]:
            add("export_curtailed", "warning", f"Export curtailed: grid voltage {now['voltage']:.0f} V")
        if prev["tariff"] != now["tariff"] and now["tariff"] in ("peak", "off_peak"):
            add("tariff_period_started", "info", f"{now['tariff'].replace('_', '-')} tariff started", period=now["tariff"])
        if not prev["unserved"] and now["unserved"]:
            add("unserved_load", "critical", "Home load not fully served")
        if now["cleaned"] and not prev["cleaned"]:
            add("panels_cleaned", "info", "Panels cleaned; soiling loss reset")
        for device_id, (state, mode, name, kind) in now["devices"].items():
            before = prev["devices"].get(device_id)
            if before is None or kind in _NOISY_DEVICE_TYPES:
                continue
            if mode in ("load_shed", "power_cut") and before[1] != mode:
                add("device_shed", "warning", f"{name} switched off ({mode})", device_id=device_id)
            elif before[0] != "on" and state == "on":
                add("device_on", "info", f"{name} turned on ({mode})", device_id=device_id, mode=mode)
            elif before[0] == "on" and state != "on" and mode not in ("load_shed", "power_cut"):
                add("device_off", "info", f"{name} turned off", device_id=device_id)
        return events

    # ---- tick --------------------------------------------------------------------

    async def step(self, ts: datetime) -> TelemetryRecord:
        cfg = self.config
        tz = ZoneInfo(cfg.timezone)
        local = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
        scenario = self.scenario_for(local)

        weather: WeatherRecord = await self.weather_client.get_weather(local, scenario)
        snap = self.solar.compute(local, weather, scenario)
        demand_kw, device_readings = self.load.generate(local, weather, scenario)

        outage_cause = grid_outage_cause(cfg, local, scenario)
        grid_up = outage_cause is None
        feeder_v = feeder_voltage_v(cfg, local, scenario) if grid_up else None
        volt_watt_cap = export_limit_kw(cfg, feeder_v) if feeder_v is not None else None
        period, _ = tariff_period(cfg, local)

        # A grid-tied inverter without a working battery must shut down when
        # the grid fails (anti-islanding, IEEE 1547 / IS 16169).
        anti_islanding = not grid_up and cfg.grid_available and not self.battery.available
        solar_kw = 0.0 if anti_islanding else snap.ac_kw

        floor_soc, grid_charge_kw = self._battery_policy(period, grid_up)
        max_discharge = self.battery.max_discharge_kw(floor_soc)
        shed_ids: list[str] = []
        if not grid_up:
            supply = min(cfg.inverter_capacity_kw, solar_kw + max_discharge)
            demand_kw, device_readings, shed_ids = self._shed_for_outage(demand_kw, device_readings, supply)

        flows = dispatch_energy(
            solar_kw=solar_kw,
            load_kw=demand_kw,
            max_charge_kw=self.battery.max_charge_kw(),
            max_discharge_kw=max_discharge,
            grid_available=grid_up,
            zero_export_mode=cfg.zero_export_mode,
            zero_export_limit_kw=cfg.zero_export_limit_kw,
            battery_can_charge_from_grid=cfg.battery_can_charge_from_grid,
            grid_charge_kw=grid_charge_kw,
            export_limit_kw=volt_watt_cap,
            backup_output_limit_kw=cfg.inverter_capacity_kw,
        )

        actual_solar = flows.solar_to_home_kw + flows.solar_to_battery_kw + flows.solar_to_grid_kw
        served_kw = flows.solar_to_home_kw + flows.battery_to_home_kw + flows.grid_to_home_kw

        solar_fields = self.solar.accumulate(local, actual_solar)
        load_fields = self.load.record_served(served_kw, local, demand_kw)
        battery_fields = self.battery.apply(
            flows.solar_to_battery_kw + flows.grid_to_battery_kw,
            flows.battery_to_home_kw,
            weather,
            scenario,
            local,
        )
        grid_fields = self.grid.apply(
            local,
            flows.grid_to_home_kw + flows.grid_to_battery_kw,
            flows.solar_to_grid_kw,
            grid_up,
            scenario,
            feeder_v,
            outage_cause,
        )
        self._update_daily(local, flows, demand_kw)

        balance = validate_energy_balance(
            solar_kw=actual_solar,
            grid_import_kw=float(grid_fields["grid_import_power_kw"]),
            battery_discharge_kw=float(battery_fields["battery_discharge_power_kw"]),
            home_consumption_kw=served_kw,
            grid_export_kw=float(grid_fields["grid_export_power_kw"]),
            battery_charge_kw=float(battery_fields["battery_charge_power_kw"]),
            charge_efficiency=cfg.battery_charge_efficiency,
            discharge_efficiency=cfg.battery_discharge_efficiency,
            tolerance_kw=cfg.energy_balance_tolerance_kw,
        )

        data_quality = weather.data_quality
        if scenario == "sensor_failure":
            data_quality = "degraded"
        elif data_quality not in {"fallback", "degraded"}:
            data_quality = "simulated"

        # Inverter status reflects what dispatch actually did.
        inverter_status = snap.inverter_status
        volt_watt_active = bool(
            volt_watt_cap is not None and flows.solar_curtailed_kw > 0.01 and not cfg.zero_export_mode
        )
        if anti_islanding and snap.ac_kw > 0:
            inverter_status = "anti_islanding_shutdown"
        elif volt_watt_active:
            inverter_status = "volt_watt_curtailment"
        elif flows.solar_curtailed_kw > 0.01:
            inverter_status = "export_limited" if grid_up else "curtailed_no_load"

        solar_today = float(solar_fields["solar_energy_today_kwh"])
        consumption_today = float(load_fields["home_consumption_today_kwh"])
        export_today = float(grid_fields["grid_export_today_kwh"])
        import_cost = grid_fields["grid_import_cost_today_inr"]
        export_credit = grid_fields["grid_export_credit_today_inr"]
        net_cost = None
        if import_cost is not None:
            net_cost = round(import_cost - (export_credit or 0.0), 3)

        is_night = not (weather.is_day if weather.is_day is not None else snap.ac_kw > 0)
        conditions: list[str] = []
        if scenario != "normal_day":
            conditions.append(f"scenario:{scenario}")
        if weather.day_regime:
            conditions.append(f"weather:{weather.day_regime}")
        if not grid_up:
            conditions.append(f"grid_down:{outage_cause}")
        if anti_islanding and snap.ac_kw > 0:
            conditions.append("anti_islanding_shutdown")
        if shed_ids:
            conditions.append("load_shedding_by_inverter")
        if flows.unserved_load_kw > 0.02:
            conditions.append("unserved_load")
        if battery_fields["battery_fault_code"]:
            conditions.append("battery_fault")
        if battery_fields["battery_derate_reason"] in ("high_temperature", "low_temperature", "over_temperature_fault"):
            conditions.append(f"battery_{battery_fields['battery_derate_reason']}")
        if inverter_status == "clipping":
            conditions.append("inverter_clipping")
        if volt_watt_active:
            conditions.append("grid_over_voltage_curtailment")
        if snap.cloud_shadow:
            conditions.append("cloud_shadow")
        if snap.soiling_loss_percent >= 10.0:
            conditions.append("panels_soiled")
        if snap.shading_loss_percent >= 20.0:
            conditions.append("partial_shading")
        if grid_fields["tariff_period"] == "peak":
            conditions.append("peak_tariff")
        if weather.temperature_c >= 40.0:
            conditions.append("extreme_heat")
        if weather.precipitation_mm >= 0.5:
            conditions.append("raining")
        if is_night:
            conditions.append("night")

        device_dump = [item.model_dump(mode="json") for item in device_readings]
        snapshot = {
            "scenario": scenario,
            "grid_up": grid_up,
            "outage_cause": outage_cause,
            "outage_minutes": grid_fields["grid_outage_minutes_today"],
            "soc": float(battery_fields["battery_soc_percent"]),
            "floor": floor_soc,
            "battery_present": bool(battery_fields["battery_present"]),
            "fault": battery_fields["battery_fault_code"],
            "derate": battery_fields["battery_derate_reason"],
            "producing": actual_solar > 0.02 or snap.ac_kw > 0.02,
            "inverter": inverter_status,
            "volt_watt": volt_watt_active,
            "voltage": grid_fields["grid_voltage_v"],
            "tariff": grid_fields["tariff_period"],
            "unserved": flows.unserved_load_kw > 0.02,
            "cleaned": self.solar.cleaned_today,
            "devices": {
                d.device_id: (d.current_state, d.operating_mode, d.device_name, d.device_type)
                for d in device_readings
            },
        }
        events = self._events(snapshot)
        self._prev = snapshot

        extra: dict[str, Any] = {
            "schema_version": cfg.schema_version,
            "simulation_time": local.isoformat(),
            "wall_clock_time": datetime.now(tz=tz).isoformat(),
            "scenario": scenario,
            "scenario_description": SCENARIO_INFO.get(scenario),
            "active_conditions": conditions,
            "events": events,
            # Solar / inverter
            "solar_potential_kw": round(snap.ac_kw, 4),
            "solar_curtailed_kw": flows.solar_curtailed_kw,
            "solar_dc_power_kw": snap.dc_kw,
            "solar_clipped_kw": snap.clipped_kw,
            "solar_peak_today_kw": round(self.solar.peak_today_kw, 4),
            "poa_irradiance_wm2": snap.poa_irradiance_wm2,
            "pv_cell_temperature_c": snap.cell_temperature_c,
            "pv_temperature_loss_percent": snap.temperature_loss_percent,
            "pv_soiling_loss_percent": snap.soiling_loss_percent,
            "pv_shading_loss_percent": snap.shading_loss_percent,
            "panel_days_since_cleaning": self.solar.days_since_cleaning,
            "performance_ratio": snap.performance_ratio,
            "inverter_status": inverter_status,
            "inverter_efficiency_percent": snap.inverter_efficiency_percent,
            "cloud_shadow": snap.cloud_shadow,
            "irradiance_source": snap.irradiance_source,
            # Home
            "home_load_served_kw": round(served_kw, 4),
            "home_load_peak_today_kw": round(self.load.peak_today_kw, 4),
            "load_breakdown_kw": dict(self.load.last_breakdown),
            "occupancy_level": self.load.last_occupancy,
            "shed_device_ids": shed_ids,
            # Battery
            "battery_power_kw": battery_fields["battery_power_kw"],
            "battery_temperature_c": battery_fields["battery_temperature_c"],
            "battery_fault_code": battery_fields["battery_fault_code"],
            "battery_charge_interval_kwh": battery_fields["battery_charge_interval_kwh"],
            "battery_discharge_interval_kwh": battery_fields["battery_discharge_interval_kwh"],
            "battery_charge_today_kwh": battery_fields["battery_charge_today_kwh"],
            "battery_discharge_today_kwh": battery_fields["battery_discharge_today_kwh"],
            "battery_capacity_effective_kwh": battery_fields["battery_capacity_effective_kwh"],
            "battery_cycle_count": battery_fields["battery_cycle_count"],
            "battery_derate_reason": battery_fields["battery_derate_reason"],
            "battery_present": battery_fields["battery_present"],
            "battery_discharge_floor_percent": round(floor_soc, 1),
            "grid_to_battery_kw": flows.grid_to_battery_kw,
            # Grid / tariff
            "grid_outage_cause": grid_fields["grid_outage_cause"],
            "grid_outage_minutes_today": grid_fields["grid_outage_minutes_today"],
            "grid_import_interval_kwh": grid_fields["grid_import_interval_kwh"],
            "grid_export_interval_kwh": grid_fields["grid_export_interval_kwh"],
            "grid_import_today_kwh": grid_fields["grid_import_today_kwh"],
            "grid_export_today_kwh": export_today,
            "grid_voltage_v": grid_fields["grid_voltage_v"],
            "grid_frequency_hz": grid_fields["grid_frequency_hz"],
            "export_limit_kw": round(volt_watt_cap, 3) if volt_watt_cap is not None else None,
            "tariff_type": cfg.tariff_type,
            "tariff_period": grid_fields["tariff_period"],
            "tariff_rate_inr_per_kwh": grid_fields["tariff_rate_inr_per_kwh"],
            "export_credit_inr_per_kwh": grid_fields["export_credit_inr_per_kwh"],
            "grid_import_cost_interval_inr": grid_fields["grid_import_cost_interval_inr"],
            "grid_export_credit_interval_inr": grid_fields["grid_export_credit_interval_inr"],
            "grid_import_cost_today_inr": import_cost,
            "grid_export_credit_today_inr": export_credit,
            "net_energy_cost_today_inr": net_cost,
            # Daily KPIs
            "self_consumption_percent_today": _pct(solar_today - export_today, solar_today),
            "self_sufficiency_percent_today": _pct(
                consumption_today - self._daily["grid_to_home_kwh"], consumption_today
            ),
            "unserved_energy_today_kwh": round(self._daily["unserved_kwh"], 4),
            "solar_curtailed_today_kwh": round(self._daily["curtailed_kwh"], 4),
            # Context
            "primary_goal": cfg.primary_goal,
            "system": {
                "system_type": cfg.system_type,
                "solar_capacity_kwp": cfg.solar_capacity_kwp,
                "inverter_capacity_kw": cfg.inverter_capacity_kw,
                "battery_capacity_kwh": cfg.battery_capacity_kwh if cfg.battery_present else 0.0,
                "panel_tilt_deg": round(cfg.tilt_deg, 1),
                "panel_azimuth_deg": round(cfg.azimuth_deg, 1),
                "grid_connected": cfg.grid_available,
                "zero_export_mode": cfg.zero_export_mode,
            },
            "system_losses_kw": balance.system_losses_kw,
            "warnings": list(balance.warnings),
            "location": self.home_location.model_dump(mode="json"),
        }

        record = TelemetryRecord(
            timestamp=local,
            household_id=cfg.household_id,
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
            devices=device_dump,
            weather=weather.model_dump(mode="json"),
            **extra,
        )
        self.write_record(record)
        bus.publish(record.model_dump(mode="json"))
        return record
