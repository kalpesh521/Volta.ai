"""
Batched upsert against a real TimescaleDB. Opt-in, like the other database tests:

    TEST_TIMESCALE_URL=postgresql+asyncpg://volta:volta@127.0.0.1:5433/<scratch db> pytest tests/test_ingest_batch_timescale.py

Use a scratch database (the test creates `telemetry_ticks` there and removes only its own rows).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import DBAPIError

from app.modules.energy.schemas import TelemetryRecord
from app.workers import timescale_batch
from app.workers.ingest_batch import BatchSettings, IngestBatcher, is_transient_db_error
from app.workers.timescale_batch import BatchingTimescaleStore
from tests.test_ingest_batch import BASE, FakeMessage, _settled
from tests.test_ingest_worker import _tick

TIMESCALE_URL = os.environ.get("TEST_TIMESCALE_URL", "")
pytestmark = pytest.mark.skipif(not TIMESCALE_URL, reason="TEST_TIMESCALE_URL is not set")


def _record(household: str, minute: int, solar: float = 3.0) -> TelemetryRecord:
    stamp = (BASE + timedelta(minutes=minute)).isoformat()
    return TelemetryRecord.model_validate(
        _tick(
            timestamp=stamp,
            household_id=household,
            solar_power_kw=solar,
            solar_energy_interval_kwh=solar / 60.0,
            solar_to_home_kw=1.5,
            solar_to_battery_kw=solar - 1.5,
            battery_charge_power_kw=solar - 1.5,
        )
    )


@asynccontextmanager
async def scratch_store():
    """Created inside the test, never in a fixture: an async engine must be used on the loop that opened it."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(TIMESCALE_URL, pool_pre_ping=True)
    backend = BatchingTimescaleStore(engine=engine)
    await backend.connect()
    await backend.ensure_schema()
    household = f"p5-{uuid.uuid4().hex[:10]}"
    try:
        yield backend, household
    finally:
        from sqlalchemy import text

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM telemetry_ticks WHERE household_id LIKE 'p5-%'"))
        await backend.close()


async def _count(backend: BatchingTimescaleStore, household: str) -> int:
    rows = await backend.fetch_range(household, BASE - timedelta(days=1), BASE + timedelta(days=1))
    return len(rows)


async def test_a_batch_commits_every_row_and_replaying_it_changes_nothing():
    async with scratch_store() as (backend, household):
        records = [_record(household, i) for i in range(30)]

        await backend.upsert_many(records)
        assert await _count(backend, household) == 30

        await backend.upsert_many(records)  # redelivery after a crash between commit and ack
        assert await _count(backend, household) == 30


async def test_an_upsert_overwrites_the_same_tick_with_the_newer_values():
    async with scratch_store() as (backend, household):
        await backend.upsert_many([_record(household, 0, solar=3.0)])
        await backend.upsert_many([_record(household, 0, solar=4.0)])
        (row,) = await backend.fetch_range(household, BASE - timedelta(hours=1), BASE + timedelta(hours=1))
        assert row.solar_power_kw == 4.0


async def test_a_failing_row_rolls_back_the_whole_batch_and_is_not_a_transient_error(monkeypatch):
    async with scratch_store() as (backend, household):
        real = timescale_batch.record_to_params

        def poisoned(record):
            params = real(record)
            if record.timestamp == BASE + timedelta(minutes=2):
                params["solar_power_kw"] = None  # NOT NULL column
            return params

        monkeypatch.setattr(timescale_batch, "record_to_params", poisoned)

        with pytest.raises(DBAPIError) as caught:
            await backend.upsert_many([_record(household, i) for i in range(4)])

        assert await _count(backend, household) == 0  # all or nothing
        assert not is_transient_db_error(caught.value)  # a data error: the worker retries tick by tick


async def test_two_competing_batchers_with_redelivered_duplicates_store_each_tick_exactly_once():
    async with scratch_store() as (backend, household):
        log: list = []
        replicas = [
            IngestBatcher(
                write_many=backend.upsert_many,
                tolerance_kw=0.05,
                batch=BatchSettings(batch_size=7, max_age_seconds=0.05, max_buffer=14, backoff_base_seconds=0.0),
            )
            for _ in range(2)
        ]
        for replica in replicas:
            replica.start()

        def body(minute: int) -> bytes:
            stamp = (BASE + timedelta(minutes=minute)).isoformat()
            return json.dumps(_tick(timestamp=stamp, household_id=household)).encode()

        messages = []
        for minute in range(40):
            # Every tick is delivered twice, to alternating replicas (publisher retry / redelivery).
            for copy in range(2):
                message = FakeMessage(body(minute), log, f"{minute}-{copy}")
                messages.append(message)
        await asyncio.gather(*(replicas[i % 2].submit(message) for i, message in enumerate(messages)))
        await _settled(*messages, timeout=10)
        for replica in replicas:
            await replica.drain(5)

        assert {m.outcome for m in messages} == {"ack"}
        assert await _count(backend, household) == 40
