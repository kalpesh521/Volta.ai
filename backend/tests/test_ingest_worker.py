"""Ingest worker pipeline tests. No RabbitMQ required."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.modules.energy.pipeline import process_telemetry_body
from app.core.config import settings

TZ = ZoneInfo("Asia/Kolkata")
TOLERANCE = settings.ENERGY_BALANCE_TOLERANCE_KW


def _tick(**overrides) -> dict:
    ts = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)
    interval_h = 1.0 / 60.0
    payload = {
        "timestamp": ts.isoformat(),
        "household_id": "home_001",
        "data_source": "simulator",
        "data_quality": "simulated",
        "solar_power_kw": 3.0,
        "solar_energy_interval_kwh": 3.0 * interval_h,
        "solar_energy_today_kwh": 4.2,
        "solar_energy_total_kwh": 120.0,
        "home_load_power_kw": 1.5,
        "home_consumption_interval_kwh": 1.5 * interval_h,
        "home_consumption_today_kwh": 3.1,
        "home_consumption_total_kwh": 90.0,
        "battery_soc_percent": 62.0,
        "battery_soh_percent": 98.0,
        "battery_charge_power_kw": 1.5,
        "battery_discharge_power_kw": 0.0,
        "battery_energy_available_kwh": 5.0,
        "battery_status": "charging",
        "grid_status": "available",
        "grid_import_power_kw": 0.0,
        "grid_export_power_kw": 0.0,
        "total_import_kwh": 10.0,
        "total_export_kwh": 8.0,
        "solar_to_home_kw": 1.5,
        "solar_to_battery_kw": 1.5,
        "solar_to_grid_kw": 0.0,
        "battery_to_home_kw": 0.0,
        "grid_to_home_kw": 0.0,
        "unserved_load_kw": 0.0,
        "energy_balance_status": "valid",
        "energy_balance_error_kw": 0.0,
        "energy_balance_valid": True,
        "devices": [],
        "weather": {},
    }
    payload.update(overrides)
    return payload


def test_valid_tick_is_acked():
    import json

    decision = process_telemetry_body(json.dumps(_tick()).encode(), TOLERANCE)
    assert decision.action == "ack"
    assert decision.record is not None
    assert decision.record.household_id == "home_001"
    assert decision.record.energy_balance_valid is True


def test_empty_and_invalid_json_are_rejected():
    assert process_telemetry_body(b"", TOLERANCE).action == "reject"
    assert process_telemetry_body(b"   ", TOLERANCE).reason == "empty_body"
    decision = process_telemetry_body(b"not-json", TOLERANCE)
    assert decision.action == "reject"
    assert decision.reason.startswith("invalid_json")
    decision = process_telemetry_body(b"[1,2]", TOLERANCE)
    assert decision.reason == "payload_not_object"


def test_schema_violations_are_rejected():
    import json

    bad = _tick(solar_power_kw=-1.0)
    decision = process_telemetry_body(json.dumps(bad).encode(), TOLERANCE)
    assert decision.action == "reject"
    assert decision.reason.startswith("validation_error")

    both = _tick(grid_import_power_kw=1.0, grid_export_power_kw=0.5, solar_to_battery_kw=0.5)
    # keep energy numbers consistent enough; invariant is import+export
    decision = process_telemetry_body(json.dumps(both).encode(), TOLERANCE)
    assert decision.action == "reject"


def test_unbalanced_tick_is_acked_with_warning():
    import json

    # solar 5, load 1, charge 0 → AC-bus mismatch, HTTP ingest still 202
    payload = _tick(
        solar_power_kw=5.0,
        solar_energy_interval_kwh=5.0 / 60.0,
        home_load_power_kw=1.0,
        home_consumption_interval_kwh=1.0 / 60.0,
        battery_charge_power_kw=0.0,
        solar_to_home_kw=1.0,
        solar_to_battery_kw=0.0,
        solar_to_grid_kw=0.0,
    )
    decision = process_telemetry_body(json.dumps(payload).encode(), TOLERANCE)
    assert decision.action == "ack"
    assert decision.record is not None
    assert decision.record.energy_balance_valid is False
    assert decision.record.energy_balance_status == "warning"
