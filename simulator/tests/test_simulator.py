"""Suryaa solar-home telemetry simulator tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from simulator.battery_engine import BatteryEngine
from simulator.config import SimulatorConfig
from simulator.energy_balance import dispatch_energy, validate_energy_balance
from simulator.grid_engine import GridEngine, grid_is_available
from simulator.load_generator import LoadGenerator
from simulator.models import WeatherRecord
from simulator.solar_generator import SolarGenerator
from simulator.telemetry_generator import TelemetryGenerator
from simulator.weather_client import WeatherClient, fallback_weather

TZ = ZoneInfo("Asia/Kolkata")


def _cfg(**overrides) -> SimulatorConfig:
    base = dict(
        weather_mode="fallback",
        timezone="Asia/Kolkata",
        latitude=18.6298,
        longitude=73.7997,
        random_seed=42,
        simulation_interval_seconds=60,
        scenario="normal_day",
    )
    base.update(overrides)
    return SimulatorConfig(**base)


def _weather(
    ts: datetime,
    *,
    ghi: float = 800.0,
    cloud: float = 10.0,
    rain: float = 0.0,
    temp: float = 28.0,
) -> WeatherRecord:
    sunrise = ts.replace(hour=6, minute=10, second=0, microsecond=0)
    sunset = ts.replace(hour=18, minute=45, second=0, microsecond=0)
    return WeatherRecord(
        timestamp=ts,
        temperature_c=temp,
        humidity_percent=55.0,
        cloud_cover_percent=cloud,
        precipitation_mm=rain,
        precipitation_probability_percent=10.0,
        wind_speed_kmh=9.0,
        shortwave_radiation_wm2=ghi,
        direct_radiation_wm2=ghi * 0.7,
        diffuse_radiation_wm2=ghi * 0.3,
        weather_code=0,
        sunrise=sunrise,
        sunset=sunset,
        source="test",
        data_quality="simulated",
    )


def test_solar_is_zero_at_night():
    ts = datetime(2026, 8, 23, 2, 0, tzinfo=TZ)
    weather = _weather(ts, ghi=0.0)
    power = SolarGenerator(_cfg()).compute_power_kw(ts, weather)
    assert power == 0.0


def test_cloudy_weather_reduces_solar_output():
    ts = datetime(2026, 8, 23, 12, 30, tzinfo=TZ)
    gen = SolarGenerator(_cfg())
    clear = gen.compute_power_kw(ts, _weather(ts, ghi=800.0, cloud=8.0))
    cloudy = gen.compute_power_kw(ts, _weather(ts, ghi=320.0, cloud=90.0))
    assert clear > 0
    assert cloudy < clear


def test_measured_ghi_is_not_cloud_or_rain_derated_again():
    ts = datetime(2026, 8, 23, 12, 30, tzinfo=TZ)
    gen = SolarGenerator(_cfg())
    clear = gen.compute_power_kw(ts, _weather(ts, ghi=800.0, cloud=8.0, rain=0.0))
    cloudy = gen.compute_power_kw(ts, _weather(ts, ghi=800.0, cloud=90.0, rain=6.0))
    assert clear == cloudy


def test_synthetic_ghi_applies_cloud_and_rain():
    ts = datetime(2026, 8, 23, 12, 30, tzinfo=TZ)
    gen = SolarGenerator(_cfg())
    clear = gen.compute_power_kw(ts, _weather(ts, ghi=0.0, cloud=8.0, rain=0.0))
    cloudy = gen.compute_power_kw(ts, _weather(ts, ghi=0.0, cloud=90.0, rain=0.0))
    wet = gen.compute_power_kw(ts, _weather(ts, ghi=0.0, cloud=8.0, rain=6.0))
    assert clear > 0
    assert cloudy < clear
    assert wet < clear


def test_solar_never_exceeds_inverter_capacity():
    ts = datetime(2026, 8, 23, 12, 30, tzinfo=TZ)
    gen = SolarGenerator(_cfg(inverter_capacity_kw=1.2, solar_capacity_kwp=5.0))
    power = gen.compute_power_kw(ts, _weather(ts, ghi=1100.0, cloud=0.0, rain=0.0))
    assert 0 <= power <= 1.2


def test_battery_never_goes_below_minimum_soc():
    config = _cfg(
        grid_available=False,
        initial_battery_soc_percent=22.0,
        battery_minimum_soc_percent=20.0,
        battery_capacity_kwh=10.0,
        battery_usable_capacity_kwh=10.0,
    )
    battery = BatteryEngine(config)
    weather = _weather(datetime(2026, 8, 23, 21, 0, tzinfo=TZ), ghi=0.0)
    for _ in range(180):
        max_dch = battery.max_discharge_kw()
        battery.apply(0.0, max_dch, weather)
        assert battery.soc_percent >= config.battery_minimum_soc_percent - 1e-6
    assert battery.soc_percent == pytest.approx(config.battery_minimum_soc_percent, abs=0.05)


def test_battery_never_exceeds_100_soc():
    config = _cfg(
        initial_battery_soc_percent=99.2,
        battery_capacity_kwh=10.0,
        battery_usable_capacity_kwh=10.0,
    )
    battery = BatteryEngine(config)
    weather = _weather(datetime(2026, 8, 23, 13, 0, tzinfo=TZ))
    for _ in range(60):
        battery.apply(battery.max_charge_kw(), 0.0, weather)
        assert battery.soc_percent <= 100.0 + 1e-9
    assert battery.soc_percent == pytest.approx(100.0, abs=0.05)


def test_grid_import_zero_during_outage():
    flows = dispatch_energy(
        solar_kw=0.4,
        load_kw=2.5,
        max_charge_kw=0.0,
        max_discharge_kw=0.3,
        grid_available=False,
        zero_export_mode=False,
        zero_export_limit_kw=0.0,
        battery_can_charge_from_grid=False,
    )
    assert flows.grid_to_home_kw == 0.0
    assert flows.unserved_load_kw > 0

    engine = GridEngine(_cfg(grid_available=False))
    ts = datetime(2026, 8, 23, 20, 0, tzinfo=TZ)
    snapshot = engine.apply(ts, import_kw=3.0, export_kw=0.4, available=False)
    assert snapshot["grid_import_power_kw"] == 0.0
    assert snapshot["grid_status"] == "outage"
    assert not grid_is_available(_cfg(grid_available=False), ts)

    both = GridEngine(_cfg()).apply(
        datetime(2026, 8, 23, 13, 0, tzinfo=TZ),
        import_kw=1.2,
        export_kw=0.8,
        available=True,
    )
    assert not (both["grid_import_power_kw"] > 0 and both["grid_export_power_kw"] > 0)


def test_grid_export_zero_in_zero_export_mode():
    flows = dispatch_energy(
        solar_kw=4.0,
        load_kw=1.0,
        max_charge_kw=0.5,
        max_discharge_kw=0.0,
        grid_available=True,
        zero_export_mode=True,
        zero_export_limit_kw=0.0,
        battery_can_charge_from_grid=False,
    )
    assert flows.solar_to_grid_kw == 0.0
    assert flows.solar_curtailed_kw == pytest.approx(2.5)

    engine = GridEngine(_cfg(zero_export_mode=True, zero_export_limit_kw=0.0))
    ts = datetime(2026, 8, 23, 13, 0, tzinfo=TZ)
    snapshot = engine.apply(ts, import_kw=0.0, export_kw=1.8, available=True)
    assert snapshot["grid_export_power_kw"] == 0.0


def test_energy_balance_within_tolerance():
    result = validate_energy_balance(
        solar_kw=4.12,
        grid_import_kw=0.0,
        battery_discharge_kw=0.0,
        home_consumption_kw=1.68,
        grid_export_kw=0.24,
        battery_charge_kw=2.20,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        tolerance_kw=0.05,
    )
    assert result.energy_balance_valid
    assert result.energy_balance_status == "valid"
    assert abs(result.energy_balance_error_kw) <= 0.05


@pytest.mark.asyncio
async def test_weather_api_failure_uses_fallback(monkeypatch):
    client = WeatherClient(_cfg(weather_mode="live"))

    async def fail_fetch(_ts: datetime) -> None:
        raise httpx.ConnectError("open-meteo unreachable")

    monkeypatch.setattr(client, "_fetch_live", fail_fetch)
    ts = datetime(2026, 8, 23, 12, 0, tzinfo=TZ)
    record = await client.get_weather(ts)
    assert record.data_quality == "fallback"
    assert record.source == "fallback"
    assert record.shortwave_radiation_wm2 >= 0


def test_device_power_affects_home_load():
    config = _cfg()
    load = LoadGenerator(config)
    ts = datetime(2026, 8, 23, 0, 5, tzinfo=TZ)
    weather = fallback_weather(ts, config)
    demand, devices = load.generate(ts, weather)
    fridge = next(item for item in devices if item.device_type == "refrigerator")
    base = load.base_load_kw(ts)
    assert fridge.current_power_kw > 0
    assert demand == pytest.approx(
        round(base + sum(d.current_power_kw for d in devices), 4)
    )
    assert demand > base


@pytest.mark.asyncio
async def test_random_seed_is_repeatable():
    start = datetime(2026, 8, 23, 12, 0, tzinfo=TZ)

    async def run_once() -> list[dict]:
        gen = TelemetryGenerator(_cfg(random_seed=42, weather_mode="fallback"))
        rows = []
        ts = start
        for _ in range(5):
            record = await gen.step(ts)
            rows.append(
                record.model_dump(
                    mode="json",
                    exclude={"timestamp", "wall_clock_time"},
                )
            )
            ts += timedelta(seconds=60)
        return rows

    first = await run_once()
    second = await run_once()
    assert first == second


@pytest.mark.asyncio
async def test_full_tick_energy_balance_and_night_solar():
    gen = TelemetryGenerator(_cfg(weather_mode="fallback", grid_available=True))
    night = datetime(2026, 8, 23, 2, 30, tzinfo=TZ)
    noon = datetime(2026, 8, 23, 12, 30, tzinfo=TZ)

    night_row = await gen.step(night)
    assert night_row.solar_power_kw == 0.0
    assert night_row.energy_balance_status == "valid"

    noon_row = await gen.step(noon)
    assert noon_row.solar_power_kw >= 0
    assert noon_row.energy_balance_valid is True
    assert abs(noon_row.energy_balance_error_kw) <= 0.05
    assert noon_row.battery_soc_percent <= 100.0
    assert noon_row.battery_soc_percent >= 20.0
    assert not (
        noon_row.grid_import_power_kw > 0 and noon_row.grid_export_power_kw > 0
    )
    interval_h = gen.config.simulation_interval_seconds / 3600.0
    assert noon_row.solar_energy_interval_kwh == pytest.approx(
        noon_row.solar_power_kw * interval_h, abs=1e-5
    )
    assert getattr(noon_row, "simulation_time", None)
    assert getattr(noon_row, "wall_clock_time", None)


def test_geocoding_parses_open_meteo_result():
    from simulator.geocoding_client import apply_location, parse_result

    location = parse_result(
        {
            "id": 1259229,
            "name": "Pune",
            "latitude": 18.5203,
            "longitude": 73.8567,
            "elevation": 560.0,
            "timezone": "Asia/Kolkata",
            "country": "India",
            "country_code": "IN",
            "admin1": "Maharashtra",
        },
        query="Pune",
    )
    assert location.latitude == 18.5203
    assert location.timezone == "Asia/Kolkata"
    assert "Pune" in location.label()
    updated = apply_location(_cfg(), location)
    assert updated.latitude == 18.5203
    assert updated.longitude == 73.8567
    assert updated.timezone == "Asia/Kolkata"
    assert updated.location_name == "Pune"


@pytest.mark.asyncio
async def test_geocoding_failure_uses_configured_fallback(monkeypatch):
    from simulator.geocoding_client import GeocodingClient

    client = GeocodingClient(_cfg(location_name="Nowhereville"))

    async def fail_search(_name: str, **_kwargs):
        raise httpx.ConnectError("geocoding unreachable")

    monkeypatch.setattr(client, "search", fail_search)
    location = await client.resolve("Nowhereville")
    assert location.data_quality == "fallback"
    assert location.latitude == 18.6298
    assert location.timezone == "Asia/Kolkata"


def test_gps_forecast_meta_sets_timezone():
    from simulator.geocoding_client import parse_forecast_meta

    location = parse_forecast_meta(
        {"timezone": "Asia/Kolkata", "elevation": 12.0},
        15.2993,
        74.1240,
    )
    assert location.name == "Current location"
    assert location.source == "browser-gps"
    assert location.latitude == 15.2993
    assert location.longitude == 74.1240
    assert location.timezone == "Asia/Kolkata"
    assert location.elevation_m == 12.0


@pytest.mark.asyncio
async def test_gps_timezone_failure_keeps_coordinates(monkeypatch):
    from simulator.geocoding_client import GeocodingClient

    client = GeocodingClient(_cfg())

    async def boom(*_args, **_kwargs):
        raise httpx.ConnectError("forecast unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    location = await client.from_coordinates(15.5, 73.8)
    assert location.latitude == 15.5
    assert location.longitude == 73.8
    assert location.source == "browser-gps"
    assert location.data_quality == "fallback"
    assert location.timezone == "Asia/Kolkata"


def test_live_bus_close_drops_publish_and_wait():
    from simulator.live_bus import LiveBus

    stream = LiveBus()
    stream.close()
    seq, record = stream.wait_next(0, timeout=0.05)
    assert record is None
    stream.publish({"solar_power_kw": 1.0})
    assert stream.latest() is None
    assert stream.closed is True
