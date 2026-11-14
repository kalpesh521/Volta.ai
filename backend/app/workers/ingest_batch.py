"""
Batching core of the ingest worker (Phase 5.1). No broker code: it works on message objects that have
`body`, `headers`, `ack()`, `nack(requeue=)` and `reject(requeue=)`, so it is tested with fakes.

Delivery contract (at-least-once, duplicate-safe):

* A valid tick is buffered. It is acked only after the transaction that contains it has committed.
* A batch is flushed when it holds `batch_size` ticks, or when its oldest tick is `max_age_seconds`
  old, whichever comes first. At most `max_buffer` ticks are held: further `submit` calls wait, which
  in turn stops the broker delivering more (consumer prefetch is the outer bound).
* A tick that cannot be parsed or validated is dead-lettered at once (`reject(requeue=False)`).
* The database is unreachable or slow (transient failure): the whole batch is nack-requeued after a
  growing pause. Nothing is dropped and no attempt is counted, so an outage never dead-letters data.
* Any other failure (a bad value that the database refuses): the batch is retried tick by tick so one
  bad tick cannot hold back 24 good ones. Good ticks commit and are acked. A failing tick is requeued,
  and dead-lettered once it has failed `max_attempts` times.
* Replicas compete on one queue. The same tick may arrive twice (redelivery after a lost ack, a crash
  between commit and ack, or the producer publishing it twice). The write is an upsert on
  (household_id, time), so a duplicate rewrites the same row. Duplicates inside one batch are collapsed.
* `drain()` is the SIGTERM path: stop accepting, flush everything buffered, return. Whatever cannot be
  flushed in time is nack-requeued, never silently dropped.

The Redis live snapshot is published AFTER the ack and is best effort (history is already durable).
A hung Redis therefore delays the next flush by at most `redis_timeout`, but never an ack.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from opentelemetry.trace import Link, SpanContext

from app.core.config import settings
from app.core.metrics import INGEST_MESSAGE_AGE_SECONDS, INGEST_MESSAGES, INGEST_SECONDS
from app.core.telemetry import consumer_span, get_tracer
from app.modules.energy.pipeline import process_telemetry_body
from app.modules.energy.schemas import TelemetryRecord
from app.workers.ingest_metrics import (
    INGEST_BATCH_SIZE,
    INGEST_BUFFERED,
    INGEST_FLUSH_SECONDS,
    INGEST_FLUSHES,
)

logger = logging.getLogger("volta.ingest.batch")

_MAX_SPAN_LINKS = 32
_MAX_TRACKED_ATTEMPTS = 10_000

WriteMany = Callable[[Sequence[TelemetryRecord]], Awaitable[None]]
LivePublish = Callable[[TelemetryRecord], Awaitable[None]]
TickKey = tuple[str, datetime]


class IncomingMessage(Protocol):
    body: bytes
    headers: Any

    async def ack(self) -> None: ...
    async def nack(self, requeue: bool = ...) -> None: ...
    async def reject(self, requeue: bool = ...) -> None: ...


@dataclass(frozen=True)
class BatchSettings:
    batch_size: int = 25
    max_age_seconds: float = 0.5
    max_buffer: int = 50
    write_timeout_seconds: float = 30.0
    max_attempts: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    redis_timeout_seconds: float = 5.0

    @classmethod
    def from_app_settings(cls) -> "BatchSettings":
        """Read the INGEST_* settings, clamped to values that cannot wedge the worker."""
        prefetch = max(1, settings.RABBITMQ_PREFETCH)
        requested = max(1, settings.INGEST_BATCH_SIZE)
        if requested > prefetch:
            logger.warning(
                "INGEST_BATCH_SIZE=%d is larger than RABBITMQ_PREFETCH=%d; using %d "
                "(the broker never delivers more unacked messages than the prefetch)",
                requested,
                prefetch,
                prefetch,
            )
        elif prefetch < 2 * requested:
            logger.warning(
                "RABBITMQ_PREFETCH=%d is below 2 x INGEST_BATCH_SIZE=%d: reading will stall while a batch is written",
                prefetch,
                requested,
            )
        return cls(
            batch_size=min(requested, prefetch),
            max_age_seconds=max(0.01, settings.INGEST_BATCH_MAX_AGE_SECONDS),
            max_buffer=prefetch,
            write_timeout_seconds=max(0.1, settings.INGEST_WRITE_TIMEOUT_SECONDS),
            max_attempts=max(1, settings.INGEST_MAX_ATTEMPTS),
            backoff_base_seconds=max(0.0, settings.INGEST_RETRY_BACKOFF_SECONDS),
            backoff_max_seconds=max(0.0, settings.INGEST_RETRY_BACKOFF_MAX_SECONDS),
        )


def is_transient_db_error(exc: BaseException) -> bool:
    """
    True when retrying the same data later is expected to work: the connection, the server or a lock,
    not the data. Everything unrecognised is treated as NOT transient so that it is counted and
    eventually dead-lettered instead of retried forever.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    for candidate in (exc, getattr(exc, "orig", None), getattr(getattr(exc, "orig", None), "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(state, str) and state[:2] in {"08", "40", "53", "57", "58"}:
            # 08 connection, 40 serialization/deadlock, 53 out of resources, 57 shutdown, 58 system
            return True
    return False


@dataclass
class _Pending:
    message: IncomingMessage
    record: TelemetryRecord
    key: TickKey
    received_at: float
    delivery_count: int
    span_context: SpanContext | None = None
    settled: bool = False  # acked, nacked or rejected: never settle a delivery twice (the broker closes the channel)


@dataclass
class _Stats:
    stored: int = 0
    flushes: int = 0
    transient_failures: int = 0
    dead_lettered: int = 0
    requeued: int = 0
    deduplicated: int = 0
    rejected: int = 0
    last_error: str = ""


def _tick_key(record: TelemetryRecord) -> TickKey:
    stamp = record.timestamp if record.timestamp.tzinfo else record.timestamp.replace(tzinfo=timezone.utc)
    return record.household_id, stamp


def _age_seconds(timestamp: datetime) -> float:
    stamp = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - stamp.timestamp())


def _delivery_count(message: IncomingMessage) -> int:
    """Prior deliveries according to the broker: x-delivery-count exists on quorum queues only."""
    headers = getattr(message, "headers", None) or {}
    try:
        return max(0, int(headers.get("x-delivery-count", 0)))
    except (TypeError, ValueError):
        return 0


class IngestBatcher:
    def __init__(
        self,
        *,
        write_many: WriteMany,
        tolerance_kw: float,
        batch: BatchSettings | None = None,
        live_publish: LivePublish | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._write_many = write_many
        self._tolerance_kw = tolerance_kw
        self._cfg = batch or BatchSettings()
        self._live_publish = live_publish
        self._clock = clock
        self._sleep = sleep

        self._buffer: list[_Pending] = []
        self._inflight: list[_Pending] = []
        self._wake = asyncio.Event()
        self._space = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._consecutive_failures = 0
        self._attempts: OrderedDict[TickKey, int] = OrderedDict()
        self.stats = _Stats()

    # ------------------------------------------------------------------ intake

    @property
    def buffered(self) -> int:
        return len(self._buffer) + len(self._inflight)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="ingest-flusher")

    async def submit(self, message: IncomingMessage) -> None:
        """Validate one delivery and buffer it. Never raises: the broker callback must not die."""
        try:
            await self._submit(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ingest submit failed, requeue")
            await self._settle(message, requeue=True)
            INGEST_MESSAGES.labels(outcome="requeued").inc()

    async def _submit(self, message: IncomingMessage) -> None:
        if self._closing:
            # Shutting down: leave it for another replica.
            await self._settle(message, requeue=True)
            INGEST_MESSAGES.labels(outcome="requeued").inc()
            return
        received_at = self._clock()
        with consumer_span(
            "ingest.process",
            message.headers,
            **{"messaging.system": "rabbitmq", "messaging.destination.name": settings.RABBITMQ_QUEUE},
        ) as span:
            decision = process_telemetry_body(message.body, self._tolerance_kw)
            if decision.action == "reject" or decision.record is None:
                logger.warning("dead-letter telemetry: %s", decision.reason)
                INGEST_MESSAGES.labels(outcome="rejected").inc()
                self.stats.rejected += 1
                await self._settle(message, requeue=False, reject=True)
                return
            ctx = span.get_span_context()
            pending = _Pending(
                message=message,
                record=decision.record,
                key=_tick_key(decision.record),
                received_at=received_at,
                delivery_count=_delivery_count(message),
                span_context=ctx if ctx.is_valid else None,
            )
        async with self._space:
            while len(self._buffer) >= self._cfg.max_buffer and not self._closing:
                await self._space.wait()  # bounded memory: backpressure instead of growing
            if self._closing:
                requeue = True
            else:
                self._buffer.append(pending)
                INGEST_BUFFERED.set(self.buffered)
                requeue = False
        if requeue:
            await self._settle(message, requeue=True)
            INGEST_MESSAGES.labels(outcome="requeued").inc()
            return
        self._wake.set()

    # ------------------------------------------------------------------ flushing

    async def _run(self) -> None:
        while True:
            if not self._buffer:
                if self._closing:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            age = self._clock() - self._buffer[0].received_at
            if not self._closing and len(self._buffer) < self._cfg.batch_size and age < self._cfg.max_age_seconds:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._cfg.max_age_seconds - age)
                except asyncio.TimeoutError:
                    pass
                continue
            async with self._space:
                batch = self._buffer[: self._cfg.batch_size]
                del self._buffer[: self._cfg.batch_size]
                self._inflight = batch
                self._space.notify_all()
            try:
                await self._flush(batch)
            except asyncio.CancelledError:
                raise  # drain() settles whatever is still unsettled in self._inflight
            except Exception:
                logger.exception("ingest flush crashed, requeue unsettled ticks of %d", len(batch))
                await self._requeue([item for item in batch if not item.settled])
            self._inflight = []
            INGEST_BUFFERED.set(self.buffered)

    async def _flush(self, batch: list[_Pending]) -> None:
        unique: OrderedDict[TickKey, _Pending] = OrderedDict()
        for item in batch:
            if item.key in unique:
                self.stats.deduplicated += 1
                INGEST_MESSAGES.labels(outcome="deduplicated").inc()
            unique[item.key] = item  # the last delivery of a tick wins, as it would across separate writes
        # Index by key so duplicate deliveries share their tick's fate.
        by_key: dict[TickKey, list[_Pending]] = {}
        for item in batch:
            by_key.setdefault(item.key, []).append(item)

        links = [
            Link(item.span_context)
            for item in list(unique.values())[:_MAX_SPAN_LINKS]
            if item.span_context is not None
        ]
        with get_tracer().start_as_current_span(
            "ingest.flush",
            links=links,
            attributes={"messaging.batch.message_count": len(batch), "ingest.batch.distinct_ticks": len(unique)},
        ):
            started = time.perf_counter()
            try:
                await self._write([item.record for item in unique.values()])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.last_error = type(exc).__name__
                if is_transient_db_error(exc):
                    await self._on_transient_failure(batch, exc)
                    return
                logger.warning("batch write failed (%s), retrying %d ticks one by one", exc, len(unique))
                INGEST_FLUSHES.labels(outcome="isolated").inc()
                committed = await self._isolate(unique, by_key)
            else:
                INGEST_FLUSH_SECONDS.observe(time.perf_counter() - started)
                INGEST_FLUSHES.labels(outcome="committed").inc()
                INGEST_BATCH_SIZE.observe(len(unique))
                self._consecutive_failures = 0
                committed = list(by_key.keys())

        self.stats.flushes += 1
        ok = [item for key in committed for item in by_key[key]]
        await self._ack_all(ok)
        for key in committed:
            self._attempts.pop(key, None)
        await self._publish_live([unique[key].record for key in committed])

    async def _write(self, records: Sequence[TelemetryRecord]) -> None:
        await asyncio.wait_for(self._write_many(records), timeout=self._cfg.write_timeout_seconds)

    async def _on_transient_failure(self, batch: list[_Pending], exc: BaseException) -> None:
        self._consecutive_failures += 1
        self.stats.transient_failures += 1
        INGEST_FLUSHES.labels(outcome="transient_failure").inc()
        logger.warning(
            "transient write failure (%s: %s); requeueing %d ticks (consecutive failures: %d)",
            type(exc).__name__,
            exc,
            len(batch),
            self._consecutive_failures,
        )
        await self._requeue(batch)
        if not self._closing and self._cfg.backoff_base_seconds > 0:
            delay = min(
                self._cfg.backoff_max_seconds,
                self._cfg.backoff_base_seconds * (2 ** (self._consecutive_failures - 1)),
            )
            await self._sleep(delay)

    async def _isolate(
        self,
        unique: "OrderedDict[TickKey, _Pending]",
        by_key: dict[TickKey, list[_Pending]],
    ) -> list[TickKey]:
        """Retry each distinct tick alone. Returns the keys that committed."""
        committed: list[TickKey] = []
        for key, item in unique.items():
            try:
                await self._write([item.record])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                group = by_key[key]
                if is_transient_db_error(exc):
                    logger.warning("transient failure while isolating household=%s: %s", key[0], exc)
                    await self._requeue(group)
                    continue
                failures = max(self._attempts.get(key, 0), *(p.delivery_count for p in group)) + 1
                self._remember_attempts(key, failures)
                if failures >= self._cfg.max_attempts:
                    logger.error(
                        "dead-letter household=%s ts=%s after %d failed writes: %s",
                        key[0],
                        key[1].isoformat(),
                        failures,
                        exc,
                    )
                    self._attempts.pop(key, None)
                    for pending in group:
                        await self._settle(pending, requeue=False, reject=True)
                        INGEST_MESSAGES.labels(outcome="dead_lettered").inc()
                        self.stats.dead_lettered += 1
                else:
                    logger.warning(
                        "write failed household=%s ts=%s attempt %d/%d: %s",
                        key[0],
                        key[1].isoformat(),
                        failures,
                        self._cfg.max_attempts,
                        exc,
                    )
                    await self._requeue(group)
            else:
                committed.append(key)
        if committed:
            INGEST_BATCH_SIZE.observe(len(committed))
        return committed

    def _remember_attempts(self, key: TickKey, count: int) -> None:
        self._attempts[key] = count
        self._attempts.move_to_end(key)
        while len(self._attempts) > _MAX_TRACKED_ATTEMPTS:
            self._attempts.popitem(last=False)

    # ------------------------------------------------------------------ settling messages

    async def _ack_all(self, items: list[_Pending]) -> None:
        for item in items:
            item.settled = True
            try:
                await item.message.ack()
            except Exception:
                # The commit already happened: a redelivery is an idempotent rewrite.
                logger.warning("ack failed after commit; the tick will be redelivered and rewritten", exc_info=True)
                continue
            INGEST_MESSAGES.labels(outcome="stored").inc()
            self.stats.stored += 1
            INGEST_MESSAGE_AGE_SECONDS.observe(_age_seconds(item.record.timestamp))
            INGEST_SECONDS.observe(max(0.0, self._clock() - item.received_at))

    async def _requeue(self, items: list[_Pending]) -> None:
        for item in items:
            if item.settled:
                continue
            await self._settle(item, requeue=True)
            INGEST_MESSAGES.labels(outcome="requeued").inc()
            self.stats.requeued += 1

    @staticmethod
    async def _settle(item: "_Pending | IncomingMessage", *, requeue: bool, reject: bool = False) -> None:
        """Nack or reject one delivery. Accepts a buffered tick or a bare message (not yet buffered)."""
        message = item.message if isinstance(item, _Pending) else item
        if isinstance(item, _Pending):
            item.settled = True
        try:
            if reject:
                await message.reject(requeue=False)
            else:
                await message.nack(requeue=requeue)
        except Exception:
            logger.warning("could not settle message (channel closed?); the broker will redeliver it", exc_info=True)

    async def _publish_live(self, records: list[TelemetryRecord]) -> None:
        if self._live_publish is None or not records:
            return
        publish = self._live_publish
        ordered = sorted(records, key=lambda record: record.timestamp)

        async def run() -> None:
            for record in ordered:
                try:
                    await publish(record)
                except Exception:
                    logger.exception(
                        "redis live update failed household=%s (tick stored in Timescale)", record.household_id
                    )

        try:
            await asyncio.wait_for(run(), timeout=self._cfg.redis_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("redis live update timed out after %.1fs; batch of %d skipped", self._cfg.redis_timeout_seconds, len(ordered))

    # ------------------------------------------------------------------ shutdown

    async def drain(self, timeout: float) -> bool:
        """
        Stop accepting, flush everything buffered, wait up to `timeout` seconds. Returns True when the
        buffer was fully flushed. Ticks still unflushed afterwards are nack-requeued.
        """
        self._closing = True
        self._wake.set()
        async with self._space:
            self._space.notify_all()
        if self._task is None:
            self.start()  # nothing was running: flush whatever a caller buffered
            self._wake.set()
        clean = True
        task = self._task
        assert task is not None
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if not done:
            clean = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif task.exception() is not None:
            clean = False
        leftover = [item for item in (*self._inflight, *self._buffer) if not item.settled]
        self._inflight, self._buffer = [], []
        if leftover:
            clean = False
            logger.warning("shutdown: requeueing %d unflushed ticks", len(leftover))
            await self._requeue(leftover)
        INGEST_BUFFERED.set(0)
        return clean
