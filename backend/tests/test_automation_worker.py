"""The automation worker's message handling: malformed input is dropped, errors never stop it, rules run on real ticks."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.agents_platform.automation.executor import RuleExecutor
from app.core.metrics import AUTOMATION_EVALUATIONS
from app.workers.automation import process_payload
from tests.assistant_helpers import make_tick
from tests.test_automation_service import HOME, active, make_user, low_battery, notifications  # noqa: F401

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def scope_of(db):
    @asynccontextmanager
    async def scope():
        yield db
        await db.flush()

    return scope


def tick(**kw) -> bytes:
    return json.dumps(make_tick(kw.pop("ts", NOW), household_id=kw.pop("household_id", HOME), **kw)).encode()


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"household_id": "home_001"}', b'{"timestamp": 5}'])
async def test_a_message_that_is_not_a_reading_is_dropped_without_error(db_session, body):
    assert await process_payload(body, RuleExecutor(cache_seconds=0), scope_of(db_session)) is None


async def test_a_real_tick_runs_the_homes_approved_rules(db_session):
    user = await make_user(db_session)
    await active(db_session, user, low_battery(soc=20.0))
    executor = RuleExecutor(cache_seconds=0)

    quiet = await process_payload(tick(soc=80.0), executor, scope_of(db_session))
    low = await process_payload(tick(soc=10.0, ts=NOW + timedelta(seconds=30)), executor, scope_of(db_session))

    assert quiet.fired == 0 and low.fired == 1 and low.notified == 1
    assert len(await notifications(db_session, "low_battery")) == 1


async def test_the_suggestion_wording_uses_the_device_names_carried_by_the_tick(db_session):
    from tests.test_automation_service import heater_when_surplus

    user = await make_user(db_session)
    await active(db_session, user, heater_when_surplus())
    devices = [{"device_id": "dev_water_heater", "device_name": "Geyser", "device_type": "water_heater", "rated_power_kw": 2.0,
                "current_state": "off", "current_power_kw": 0.0, "energy_interval_kwh": 0.0, "critical": False, "controllable": True}]

    await process_payload(tick(solar_kw=3.6, soc=75.0, devices=devices), RuleExecutor(cache_seconds=0), scope_of(db_session))

    (note,) = await notifications(db_session, "automation_suggestion")
    assert "geyser" in note.body


async def test_a_failing_evaluation_is_counted_and_does_not_raise(db_session):
    class Broken:
        async def process(self, *a, **k):
            raise RuntimeError("database down")

    before = AUTOMATION_EVALUATIONS.labels(outcome="error")._value.get()
    assert await process_payload(tick(), Broken(), scope_of(db_session)) is None
    assert AUTOMATION_EVALUATIONS.labels(outcome="error")._value.get() == before + 1
