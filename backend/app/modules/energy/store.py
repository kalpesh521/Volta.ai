"""
In-memory live telemetry store.

One latest snapshot + a bounded ring buffer per household. Thread-safe for
concurrent ingest and reads. Not durable — process restart clears state.
"""
from __future__ import annotations

import asyncio
from collections import deque

from app.core.config import settings
from app.modules.energy.schemas import TelemetryRecord


class EnergyStore:
    def __init__(self, maxlen: int | None = None) -> None:
        self._maxlen = maxlen if maxlen is not None else settings.ENERGY_HISTORY_MAX_READINGS
        self._lock = asyncio.Lock()
        self._latest: dict[str, TelemetryRecord] = {}
        self._history: dict[str, deque[TelemetryRecord]] = {}

    async def put(self, record: TelemetryRecord) -> None:
        async with self._lock:
            hid = record.household_id
            history = self._history.setdefault(hid, deque(maxlen=self._maxlen))
            history.append(record)
            current = self._latest.get(hid)
            if current is None or record.timestamp >= current.timestamp:
                self._latest[hid] = record

    async def get_latest(self, household_id: str) -> TelemetryRecord | None:
        async with self._lock:
            return self._latest.get(household_id)

    async def get_history(self, household_id: str, limit: int | None = None) -> list[TelemetryRecord]:
        async with self._lock:
            rows = list(self._history.get(household_id, ()))
        if limit is not None:
            return rows[-max(1, limit) :]
        return rows

    async def clear(self) -> None:
        async with self._lock:
            self._latest.clear()
            self._history.clear()


energy_store = EnergyStore()
