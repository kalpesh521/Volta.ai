"""A virtual command changes the next tick's device power and home load."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from simulator.clients.device_commands import command_deadline
from simulator.engines.load import LoadGenerator
from simulator.tests.test_simulator import _cfg, _weather

TZ = ZoneInfo("Asia/Kolkata")


def test_water_heater_command_raises_the_next_tick_load():
    load = LoadGenerator(_cfg())
    noon = datetime(2026, 4, 4, 12, 0, tzinfo=TZ)
    weather = _weather(noon)
    before_kw, before = load.generate(noon, weather)
    heater = next(item for item in before if item.device_type == "water_heater")
    assert heater.current_power_kw < 0.05

    assert load.device_engine.apply_command(
        "water_heater",
        "on",
        until=noon + timedelta(minutes=30),
        power_kw=2.0,
    )
    after_kw, after = load.generate(noon + timedelta(minutes=1), _weather(noon + timedelta(minutes=1)))
    forced = next(item for item in after if item.device_type == "water_heater")
    assert forced.current_state == "on"
    assert forced.current_power_kw == 2.0
    assert forced.operating_mode == "command_on"
    assert after_kw >= before_kw + 1.5


def test_command_expires_and_unknown_device_is_ignored():
    engine = LoadGenerator(_cfg()).device_engine
    start = datetime(2026, 4, 4, 12, 0, tzinfo=TZ)
    assert engine.apply_command("not_a_device", "on") is False
    assert engine.apply_command("water_heater", "on", until=start + timedelta(minutes=5))
    weather = _weather(start)
    readings_on = engine.step(start + timedelta(minutes=1), weather)
    assert next(item for item in readings_on if item.device_type == "water_heater").operating_mode == "command_on"
    readings_off = engine.step(start + timedelta(minutes=6), weather)
    heater = next(item for item in readings_off if item.device_type == "water_heater")
    assert heater.operating_mode != "command_on"


def test_command_deadline_uses_duration():
    now = datetime(2026, 4, 4, 12, 0, tzinfo=TZ)
    deadline = command_deadline({"duration_minutes": 30, "issued_at": now.isoformat()}, now)
    assert deadline == now + timedelta(minutes=30)
    assert command_deadline({}, now) is None
