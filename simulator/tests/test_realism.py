"""Physics, behaviour and data-contract checks for the realistic simulator."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from simulator.clients.weather import fallback_weather
from simulator.config import SimulatorConfig, apply_scenario
from simulator.engines.battery import BatteryEngine
from simulator.engines.climate import SyntheticClimate
from simulator.engines.grid import grid_outage_cause
from simulator.engines.sun import clear_sky, plane_of_array, solar_position, sun_times
from simulator.scenarios import SCENARIOS, scenario_for_day
from simulator.telemetry_generator import TelemetryGenerator

TZ = ZoneInfo("Asia/Kolkata")
PUNE = dict(latitude=18.6298, longitude=73.7997)


def _cfg(**overrides) -> SimulatorConfig:
    base = dict(
        weather_mode="fallback",
        timezone="Asia/Kolkata",
        random_seed=42,
        simulation_interval_seconds=60,
        scenario="normal_day",
        random_grid_outages=False,
        **PUNE,
    )
    base.update(overrides)
    return SimulatorConfig(**base)


async def _run(gen: TelemetryGenerator, start: datetime, ticks: int, step_s: int | None = None):
    step = timedelta(seconds=step_s or gen.config.simulation_interval_seconds)
    rows = []
    ts = start
    for _ in range(ticks):
        rows.append(await gen.step(ts))
        ts += step
    return rows


def _extra(row, key):
    return row.model_dump()[key]


# ---- sun / weather -----------------------------------------------------------------


def test_sunrise_sunset_match_open_meteo_for_pune():
    rise, set_ = sun_times(datetime(2026, 10, 4, 12, 0, tzinfo=TZ), **PUNE)
    assert abs((rise - datetime(2026, 10, 4, 6, 25, tzinfo=TZ)).total_seconds()) < 180
    assert abs((set_ - datetime(2026, 10, 4, 18, 20, tzinfo=TZ)).total_seconds()) < 180


def test_clear_sky_noon_and_tilted_panel_gain_in_winter():
    noon = datetime(2026, 12, 15, 12, 30, tzinfo=TZ)
    sun = solar_position(noon, **PUNE)
    sky = clear_sky(sun, 570.0)
    assert 700 < sky.ghi < 950
    poa = plane_of_array(
        ghi=sky.ghi, dni=sky.dni, dhi=sky.dhi, sun=sun, tilt_deg=19.0, surface_azimuth_deg=180.0, albedo=0.2
    )
    # South-facing tilt collects more than a flat surface when the sun is low.
    assert poa.total > sky.ghi * 1.08


def test_synthetic_weather_varies_by_day_and_is_deterministic():
    config = _cfg()
    middays = [
        fallback_weather(datetime(2026, 7, d, 13, 0, tzinfo=TZ), config, source="fallback", data_quality="fallback")
        for d in range(1, 15)
    ]
    assert len({round(w.shortwave_radiation_wm2) for w in middays}) > 5
    again = fallback_weather(datetime(2026, 7, 3, 13, 0, tzinfo=TZ), config, source="fallback", data_quality="fallback")
    assert again.shortwave_radiation_wm2 == middays[2].shortwave_radiation_wm2


def test_climate_regime_does_not_depend_on_query_order():
    kwargs = dict(latitude=18.63, longitude=73.8, elevation_m=570.0, timezone="Asia/Kolkata", seed=7)
    forward = SyntheticClimate(**kwargs)
    backward = SyntheticClimate(**kwargs)
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(90)]
    a = [forward.regime(d) for d in days]
    b = [backward.regime(d) for d in reversed(days)][::-1]
    assert a == b


def test_monsoon_is_wetter_and_darker_than_winter():
    config = _cfg()

    # Averaged over years: a single July can be a dry "break monsoon".
    def month_stats(month: int) -> tuple[float, float]:
        rain = ghi = 0.0
        years = (2024, 2025, 2026, 2027)
        for year in years:
            for d in range(1, 29):
                for h in range(24):
                    w = fallback_weather(
                        datetime(year, month, d, h, 30, tzinfo=TZ), config, source="fallback", data_quality="fallback"
                    )
                    rain += w.precipitation_mm
                    ghi += w.shortwave_radiation_wm2
        return rain / len(years), ghi / len(years)

    jan_rain, jan_ghi = month_stats(1)
    jul_rain, jul_ghi = month_stats(7)
    assert jul_rain > 100.0 > jan_rain
    assert jul_ghi < jan_ghi


# ---- battery -------------------------------------------------------------------------


def test_battery_charge_tapers_near_full():
    weather = fallback_weather(datetime(2026, 3, 1, 12, tzinfo=TZ), _cfg(), source="t", data_quality="t")
    low = BatteryEngine(_cfg(initial_battery_soc_percent=50.0))
    high = BatteryEngine(_cfg(initial_battery_soc_percent=97.0))
    low.apply(0.0, 0.0, weather)
    high.apply(0.0, 0.0, weather)
    assert high.max_charge_kw() < 0.5 * low.max_charge_kw()


def test_no_battery_reports_unavailable_not_fake_soc():
    battery = BatteryEngine(_cfg(battery_present=False))
    weather = fallback_weather(datetime(2026, 3, 1, 12, tzinfo=TZ), _cfg(), source="t", data_quality="t")
    fields = battery.apply(1.0, 0.0, weather)
    assert fields["battery_soc_percent"] == 0.0
    assert fields["battery_status"] == "unavailable"
    assert fields["battery_present"] is False


def test_battery_heats_under_load_and_overheat_scenario_trips_fault():
    weather = fallback_weather(datetime(2026, 5, 10, 14, tzinfo=TZ), _cfg(), source="t", data_quality="t")
    weather = weather.model_copy(update={"temperature_c": 38.0, "shortwave_radiation_wm2": 850.0})
    normal = BatteryEngine(_cfg(initial_battery_soc_percent=90.0))
    hot = BatteryEngine(_cfg(initial_battery_soc_percent=90.0, battery_minimum_soc_percent=0.0))
    tripped = False
    for _ in range(360):
        for battery in (normal, hot):
            if battery.soc_percent < 30.0:
                battery.soc_percent = 90.0
        normal.apply(0.0, min(2.0, normal.max_discharge_kw()), weather, "normal_day")
        hot.apply(0.0, hot.max_discharge_kw(), weather, "battery_overheat")
        tripped = tripped or hot.fault_code != 0
    assert hot.temperature_c > normal.temperature_c + 5.0
    assert tripped
    assert normal.fault_code == 0


def test_battery_soh_fades_with_throughput_and_degraded_scenario_applies():
    battery = BatteryEngine(_cfg(battery_cycle_life=100))
    weather = fallback_weather(datetime(2026, 3, 1, 21, tzinfo=TZ), _cfg(), source="t", data_quality="t")
    start = battery.soh_percent
    for _ in range(600):
        if battery.soc_percent <= 25.0:
            battery.soc_percent = 95.0
        battery.apply(0.0, 4.0, weather)
    assert battery.soh_percent < start
    degraded = apply_scenario(_cfg(), "battery_degraded")
    assert degraded.battery_soh_percent <= 72.0


# ---- grid / dispatch -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anti_islanding_shuts_down_grid_tied_pv_without_battery():
    gen = TelemetryGenerator(_cfg(battery_present=False, force_grid_outage=True, cloud_transients=False))
    row = await gen.step(datetime(2026, 3, 10, 12, 30, tzinfo=TZ))
    assert _extra(row, "solar_potential_kw") > 0.5
    assert row.solar_power_kw == 0.0
    assert row.grid_status == "outage"
    assert _extra(row, "inverter_status") == "anti_islanding_shutdown"
    assert row.unserved_load_kw == pytest.approx(row.home_load_power_kw, abs=1e-3)
    assert all(d["current_power_kw"] == 0.0 for d in row.devices)


@pytest.mark.asyncio
async def test_outage_with_battery_runs_backup_and_sheds_big_loads():
    gen = TelemetryGenerator(
        _cfg(
            force_grid_outage=True,
            initial_battery_soc_percent=80.0,
            battery_max_discharge_power_kw=0.6,
            inverter_capacity_kw=0.6,
            scenario="high_evening_load",
        )
    )
    rows = await _run(gen, datetime(2026, 5, 10, 19, 0, tzinfo=TZ), 30)
    for row in rows:
        assert row.grid_import_power_kw == 0.0
        assert row.battery_to_home_kw + row.solar_to_home_kw <= 0.6 + 1e-6
        assert row.energy_balance_valid
    shed = [d for row in rows for d in row.devices if d.get("operating_mode") == "load_shed"]
    assert shed, "air conditioner should be shed when the backup cannot carry it"
    assert all(not d["critical"] for d in shed)


@pytest.mark.asyncio
async def test_preserve_backup_keeps_reserve_while_grid_is_up():
    gen = TelemetryGenerator(
        _cfg(primary_goal="preserve_backup", battery_backup_reserve_percent=50.0, initial_battery_soc_percent=55.0)
    )
    rows = await _run(gen, datetime(2026, 5, 10, 20, 0, tzinfo=TZ), 180)
    assert min(r.battery_soc_percent for r in rows) >= 49.9
    assert rows[-1].grid_import_power_kw > 0


@pytest.mark.asyncio
async def test_tou_minimize_bill_charges_from_grid_off_peak_and_costs_more_at_peak():
    config = _cfg(
        primary_goal="minimize_bill",
        tariff_type="Time-of-use",
        tariff_rate_inr_per_kwh=8.0,
        battery_can_charge_from_grid=True,
        initial_battery_soc_percent=35.0,
    )
    gen = TelemetryGenerator(config)
    night = await gen.step(datetime(2026, 3, 10, 23, 30, tzinfo=TZ))
    assert _extra(night, "tariff_period") == "off_peak"
    assert _extra(night, "grid_to_battery_kw") > 0
    assert night.battery_discharge_power_kw == 0.0
    assert not (night.grid_import_power_kw > 0.02 and night.grid_export_power_kw > 0.02)
    peak = await TelemetryGenerator(config).step(datetime(2026, 3, 10, 19, 0, tzinfo=TZ))
    assert _extra(peak, "tariff_period") == "peak"
    assert _extra(peak, "tariff_rate_inr_per_kwh") > _extra(night, "tariff_rate_inr_per_kwh")


@pytest.mark.asyncio
async def test_voltage_rise_triggers_volt_watt_curtailment():
    gen = TelemetryGenerator(
        _cfg(
            scenario="voltage_rise",
            initial_battery_soc_percent=100.0,
            device_catalog_json="[]",
            cloud_transients=False,
        )
    )
    row = await gen.step(datetime(2026, 3, 10, 12, 30, tzinfo=TZ))
    assert _extra(row, "export_limit_kw") is not None
    assert row.model_dump()["solar_curtailed_kw"] > 0
    assert _extra(row, "inverter_status") == "volt_watt_curtailment"
    assert row.grid_export_power_kw <= _extra(row, "export_limit_kw") + 1e-3
    if row.model_dump()["grid_voltage_v"] > 253.5:
        # Street already above the limit: the inverter may not push any export.
        assert row.grid_export_power_kw == 0.0


@pytest.mark.asyncio
async def test_outage_window_emits_start_and_restore_events():
    gen = TelemetryGenerator(_cfg(grid_outage_window="12:00-12:10"))
    rows = await _run(gen, datetime(2026, 3, 10, 11, 57, tzinfo=TZ), 16)
    kinds = [e["type"] for r in rows for e in _extra(r, "events")]
    assert "grid_outage_started" in kinds
    assert "grid_restored" in kinds
    down = [r for r in rows if r.grid_status == "outage"]
    assert down and all("grid_down:scheduled_outage" in _extra(r, "active_conditions") for r in down)


def test_random_outages_are_shared_by_neighbours_and_seasonal():
    a = _cfg(random_grid_outages=True, household_id="home_a")
    b = _cfg(random_grid_outages=True, household_id="home_b")

    def outage_ticks(config, month):
        count = 0
        for d in range(1, 29):
            for m in range(0, 1440, 5):
                ts = datetime(2026, month, d, m // 60, m % 60, tzinfo=TZ)
                if grid_outage_cause(config, ts) == "feeder_fault":
                    count += 1
        return count

    assert [outage_ticks(a, m) for m in (1, 7)] == [outage_ticks(b, m) for m in (1, 7)]
    total = sum(outage_ticks(a, m) for m in range(1, 13))
    assert total > 0


def test_auto_scenario_covers_many_situations_deterministically():
    picks = [
        scenario_for_day("auto", date(2026, 1, 1) + timedelta(days=i), latitude=18.6, seed=1, synthetic_weather=True)
        for i in range(365)
    ]
    assert len(set(picks)) >= 8
    assert all(p in SCENARIOS for p in picks)
    assert "heatwave" not in {picks[i] for i in range(0, 59)}  # Jan–Feb
    again = scenario_for_day("auto", date(2026, 1, 1), latitude=18.6, seed=1, synthetic_weather=True)
    assert again == picks[0]


# ---- whole-day realism -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_season_day_has_realistic_totals_and_valid_contract():
    gen = TelemetryGenerator(_cfg(simulation_interval_seconds=300))
    rows = await _run(gen, datetime(2026, 3, 10, 0, 0, tzinfo=TZ), 288)
    last = rows[-1]
    yield_per_kwp = last.solar_energy_today_kwh / gen.config.solar_capacity_kwp
    assert 3.0 <= yield_per_kwp <= 6.5
    assert 5.0 <= last.home_consumption_today_kwh <= 35.0
    for row in rows:
        assert row.energy_balance_valid
        assert not (row.grid_import_power_kw > 0.02 and row.grid_export_power_kw > 0.02)
        assert not (row.battery_charge_power_kw > 0.02 and row.battery_discharge_power_kw > 0.02)
        assert len(row.battery_status) <= 32 and len(row.grid_status) <= 32
        assert sum(_extra(row, "load_breakdown_kw").get(k, 0.0) for k in
                   ("standby", "lighting", "fans", "kitchen", "entertainment", "misc", "tracked_devices")
                   ) == pytest.approx(row.home_load_power_kw, abs=0.01)
    night = [r for r in rows if "night" in _extra(r, "active_conditions")]
    assert night and all(r.solar_power_kw == 0.0 for r in night)
    assert _extra(last, "self_sufficiency_percent_today") is not None
    assert _extra(last, "net_energy_cost_today_inr") is not None


# ---- persistence / output -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_survives_restart(tmp_path):
    config = _cfg(state_dir=str(tmp_path))
    gen = TelemetryGenerator(config)
    rows = await _run(gen, datetime(2026, 3, 10, 10, 0, tzinfo=TZ), 30)
    path = gen.save_state()
    resumed = TelemetryGenerator(config)
    assert resumed.load_state(path)
    assert resumed.battery.soc_percent == pytest.approx(gen.battery.soc_percent)
    assert resumed.solar.energy_total_kwh == pytest.approx(rows[-1].solar_energy_total_kwh, abs=1e-5)
    nxt = await resumed.step(datetime(2026, 3, 10, 10, 30, tzinfo=TZ))
    assert nxt.solar_energy_total_kwh >= rows[-1].solar_energy_total_kwh
    assert nxt.total_import_kwh >= rows[-1].total_import_kwh


@pytest.mark.asyncio
async def test_jsonl_output_rotates(tmp_path):
    out = tmp_path / "telemetry.jsonl"
    gen = TelemetryGenerator(_cfg(output_max_mb=0.01, output_backups=2))
    gen.open_output(out)
    await _run(gen, datetime(2026, 3, 10, 10, 0, tzinfo=TZ), 12)
    gen.close_output()
    assert (tmp_path / "telemetry.jsonl.1").exists()
    assert not (tmp_path / "telemetry.jsonl.3").exists()
    for line in (tmp_path / "telemetry.jsonl.1").read_text().splitlines():
        assert json.loads(line)["household_id"] == "home_001"


def test_onboarding_profile_passes_goal_tariff_and_panel_type():
    from simulator.profile import apply_onboarding_profile

    profile = {
        "household_id": "home_tou",
        "system_type": "Hybrid",
        "panel_type": "Mono PERC",
        "solar_capacity_kwp": 5.0,
        "inverter_capacity_kw": 5.0,
        "battery_present": True,
        "battery_capacity_kwh": 10.0,
        "battery_usable_capacity_kwh": 9.0,
        "battery_minimum_soc_percent": 20.0,
        "battery_max_charge_power_kw": 3.0,
        "battery_max_discharge_power_kw": 3.0,
        "grid_available": True,
        "zero_export_mode": False,
        "primary_goal": "preserve_backup",
        "tariff_type": "Time-of-use",
        "tariff_rate": 9.5,
        "export_credit_inr_per_kwh": 2.5,
        "meter_type": "Net meter",
        "devices": [],
    }
    config = apply_onboarding_profile(_cfg(), profile)
    assert config.primary_goal == "preserve_backup"
    assert config.tariff_type == "Time-of-use"
    assert config.tariff_rate_inr_per_kwh == 9.5
    assert config.export_credit_inr_per_kwh == 2.5
    assert config.battery_can_charge_from_grid is True
    assert config.pv_temp_coefficient_per_c == pytest.approx(0.0035)
