"""Thread-safe latest-telemetry bus for the dashboard SSE stream."""

from __future__ import annotations

from collections import deque
from threading import Condition
from typing import Any


class LiveBus:
    def __init__(self, maxlen: int = 240) -> None:
        self._condition = Condition()
        self._latest: dict[str, Any] | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._closed = False

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, record: dict[str, Any]) -> None:
        with self._condition:
            if self._closed:
                return
            self._latest = record
            self._history.append(record)
            self._seq += 1
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, dict[str, Any] | None]:
        with self._condition:
            return self._seq, self._latest

    def latest(self) -> dict[str, Any] | None:
        with self._condition:
            return self._latest

    def history(self, limit: int = 120) -> list[dict[str, Any]]:
        with self._condition:
            rows = list(self._history)
        return rows[-max(1, limit) :]

    def wait_next(self, last_seq: int, timeout: float = 1.0) -> tuple[int, dict[str, Any] | None]:
        with self._condition:
            if self._closed:
                return self._seq, None
            if self._seq <= last_seq:
                self._condition.wait(timeout=timeout)
            if self._closed:
                return self._seq, None
            return self._seq, self._latest


bus = LiveBus()
