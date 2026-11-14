"""
The outbox against a real Postgres (opt-in): two relays running at once never take the same row.

    TEST_POSTGRES_URL=postgresql+asyncpg://volta:volta@localhost:5434/<scratch db> pytest tests/test_device_outbox_postgres.py

The database must be migrated to head. These tests commit (a lock only shows between two real transactions) and delete
what they inserted, so use a scratch database, never the development one.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.agents_platform.agents.device_action.models import DeviceCommandOutbox
from app.agents_platform.agents.device_action.outbox import OutboxPublisher, OutboxRelay
from app.agents_platform.agents.device_action.publisher import DeviceCommand
from app.core.config import Settings

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")


class Transport:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def publish(self, cmd: DeviceCommand) -> str:
        await asyncio.sleep(0)  # let the other relay run while this one holds its claim
        self.sent.append(cmd.command_id)
        return cmd.command_id


async def test_two_relays_never_deliver_the_same_command():
    engine = create_async_engine(POSTGRES_URL)
    household = f"outbox-{uuid.uuid4().hex[:8]}"
    ids = [uuid.uuid4().hex for _ in range(6)]
    config = Settings(DEVICE_CONTROL_ENABLED=True, DEVICE_KILL_SWITCH=False, OUTBOX_BATCH_SIZE=6)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as seed:
            for command_id in ids:
                await OutboxPublisher(seed).publish(
                    DeviceCommand(command_id, household, str(uuid.uuid4()), "water_heater", "on")
                )
            await seed.commit()

        transport_a, transport_b = Transport(), Transport()
        async with AsyncSession(engine, expire_on_commit=False) as a, AsyncSession(engine, expire_on_commit=False) as b:
            first = await OutboxRelay(transport_a, config).dispatch(a, limit=3)  # holds 3 rows locked, uncommitted
            second = await OutboxRelay(transport_b, config).dispatch(b, limit=6)  # must skip those 3
            assert first.sent == 3 and second.sent == 3, "the second relay took only what the first had not locked"
            assert not set(transport_a.sent) & set(transport_b.sent)
            await a.commit()
            await b.commit()

        assert sorted(transport_a.sent + transport_b.sent) == sorted(ids)
        async with AsyncSession(engine) as check:
            statuses = (await check.execute(select(DeviceCommandOutbox.status).where(DeviceCommandOutbox.household_id == household))).scalars().all()
            assert statuses == ["sent"] * 6
    finally:
        async with AsyncSession(engine) as cleanup:
            await cleanup.execute(delete(DeviceCommandOutbox).where(DeviceCommandOutbox.household_id == household))
            await cleanup.commit()
        await engine.dispose()


async def test_the_outbox_table_and_its_due_index_exist():
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            index = (await connection.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_device_command_outbox_due'"))).scalar_one()
            unique = (await connection.execute(text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'device_command_outbox' AND c.contype = 'u'"
            ))).scalar_one()
        assert index == 1 and unique >= 1, "command_id must be unique: it is the idempotency key"
    finally:
        await engine.dispose()
