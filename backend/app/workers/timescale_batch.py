"""
Batched writes to the telemetry hypertable.

A thin subclass of the Phase 0 store: it reuses the same SQL and the same parameter mapping, so a
batched write and a single write produce identical rows. It lives with the workers because only the
ingest worker batches.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from app.modules.energy.schemas import TelemetryRecord
from app.modules.energy.timescale_store import _UPSERT_SQL, TimescaleTelemetryStore, record_to_params


class BatchingTimescaleStore(TimescaleTelemetryStore):
    async def upsert_many(self, records: Sequence[TelemetryRecord]) -> None:
        """
        Upsert every record in ONE transaction: all of them commit or none do.

        The statement is executed once per row by the driver (asyncpg `executemany`, pipelined in a
        single round trip), which is why two records with the same (household_id, time) in one batch
        cannot trip "ON CONFLICT DO UPDATE command cannot affect row a second time". The caller still
        removes such duplicates first, so the outcome does not depend on driver ordering.
        Idempotent: replaying a batch (redelivery after a crash) rewrites the same rows.
        """
        if not records:
            return
        assert self._sessions is not None
        params = [record_to_params(record) for record in records]
        async with self._sessions() as session:
            await session.execute(text(_UPSERT_SQL), params)
            await session.commit()
