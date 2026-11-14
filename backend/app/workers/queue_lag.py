"""
Queue depth gauges for autoscaling and alerting.

Each ingest replica asks the broker how many messages are ready in the queue and how many consumers
are attached, and publishes both as gauges. The broker answer is the same for every replica, so
aggregate with `max()` in Prometheus. The read is a passive queue declare, which changes nothing.

`fetch` is injected so the logic is tested without a broker; `broker_fetch` is the aio-pika binding.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.workers.ingest_metrics import INGEST_LAG_POLL_ERRORS, INGEST_QUEUE_CONSUMERS, INGEST_QUEUE_DEPTH

logger = logging.getLogger("volta.ingest.lag")

# (messages ready, consumer count)
Fetch = Callable[[], Awaitable[tuple[int, int]]]


class QueueLagReporter:
    def __init__(self, fetch: Fetch, interval_seconds: float) -> None:
        self._fetch = fetch
        self._interval = interval_seconds

    async def poll_once(self) -> bool:
        """Update the gauges. A failure keeps the last value (a stale gauge is better than a wrong zero)."""
        try:
            ready, consumers = await self._fetch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            INGEST_LAG_POLL_ERRORS.inc()
            logger.debug("queue depth read failed: %s", exc)
            return False
        INGEST_QUEUE_DEPTH.set(ready)
        INGEST_QUEUE_CONSUMERS.set(consumers)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        if self._interval <= 0:
            return
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass


def broker_fetch(connection, queue_name: str) -> Fetch:
    """
    Read depth over a channel of its own, so a failed passive declare (it closes its channel when the
    queue is missing) cannot disturb the consuming channel. The channel is reopened on the next poll.
    """
    state: dict[str, object] = {"channel": None}

    async def fetch() -> tuple[int, int]:
        channel = state["channel"]
        if channel is None or getattr(channel, "is_closed", False):
            channel = await connection.channel()
            state["channel"] = channel
        try:
            queue = await channel.declare_queue(queue_name, passive=True)
        except Exception:
            state["channel"] = None
            raise
        result = queue.declaration_result
        return int(result.message_count), int(result.consumer_count)

    return fetch
