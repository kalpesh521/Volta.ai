"""
Telemetry ingest worker.

Consumes RabbitMQ, validates JSON, writes TimescaleDB in batches, then ACKs the batch and updates
Redis. Safe to run as any number of replicas on the one queue (competing consumers): see
`app.workers.ingest_batch` for the delivery contract (ack after commit, duplicate-safe upserts,
poison to the DLQ, transient failures requeued, SIGTERM flushes the buffered batch).

Run from backend/:

    python -m app.workers.ingest
"""
from __future__ import annotations

import asyncio
import logging
import signal

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.core.metrics import start_worker_metrics_server
from app.core.telemetry import configure_telemetry, shutdown_telemetry
from app.modules.energy.redis_live import RedisLiveStore
from app.modules.energy.timescale_store import TimescaleTelemetryStore
from app.workers.ingest_batch import BatchSettings, IngestBatcher
from app.workers.queue_lag import QueueLagReporter, broker_fetch
from app.workers.timescale_batch import BatchingTimescaleStore

logger = logging.getLogger("volta.ingest")

_QUEUE_MISMATCH_HINT = (
    "Queue {queue} already exists with different arguments (no DLX). "
    "Delete it once, then restart this worker. Test messages will be dropped:\n"
    "  docker compose exec rabbitmq rabbitmqctl delete_queue {queue} -p volta"
)


def _topic_or_fanout(name: str) -> ExchangeType:
    if name.lower() == "direct":
        return ExchangeType.DIRECT
    if name.lower() == "fanout":
        return ExchangeType.FANOUT
    return ExchangeType.TOPIC


async def declare_topology(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractQueue:
    dlx = await channel.declare_exchange(
        settings.RABBITMQ_DLX,
        ExchangeType.FANOUT,
        durable=True,
    )
    dead = await channel.declare_queue(settings.RABBITMQ_DLQ, durable=True)
    await dead.bind(dlx)

    exchange = await channel.declare_exchange(
        settings.RABBITMQ_EXCHANGE,
        _topic_or_fanout(settings.RABBITMQ_EXCHANGE_TYPE),
        durable=True,
    )
    try:
        queue = await channel.declare_queue(
            settings.RABBITMQ_QUEUE,
            durable=True,
            arguments={"x-dead-letter-exchange": settings.RABBITMQ_DLX},
        )
    except Exception as exc:
        if "PRECONDITION" in type(exc).__name__.upper() or "PRECONDITION" in str(exc).upper():
            raise RuntimeError(
                _QUEUE_MISMATCH_HINT.format(queue=settings.RABBITMQ_QUEUE)
            ) from exc
        raise
    await queue.bind(exchange, routing_key=settings.RABBITMQ_ROUTING_KEY)
    return queue


async def handle_message(
    message: AbstractIncomingMessage,
    *,
    tolerance_kw: float,
    timescale: TimescaleTelemetryStore,
    redis_live: RedisLiveStore | None,
) -> None:
    """
    Process one message on its own: a batch of one. Kept for single-message callers and tests; the
    worker itself feeds every delivery to one shared `IngestBatcher`. Same semantics: validate in a
    span that continues the producer's trace, write, ack, then publish the live snapshot.
    """

    async def write_each(records) -> None:
        for record in records:
            await timescale.upsert(record)

    batcher = IngestBatcher(
        write_many=write_each,
        tolerance_kw=tolerance_kw,
        batch=BatchSettings(batch_size=1, max_buffer=1, backoff_base_seconds=0.0),
        live_publish=redis_live.set_and_publish if redis_live is not None else None,
    )
    batcher.start()
    await batcher.submit(message)
    await batcher.drain(timeout=settings.INGEST_WRITE_TIMEOUT_SECONDS + 5)


async def run() -> None:
    url = (settings.RABBITMQ_URL or "").strip()
    if not url:
        raise RuntimeError("RABBITMQ_URL is empty")

    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    configure_telemetry(settings, service_name="volta-ingest-worker")
    start_worker_metrics_server(settings.WORKER_METRICS_PORT)

    timescale = BatchingTimescaleStore()
    await timescale.connect()
    await timescale.ensure_schema()

    redis_live: RedisLiveStore | None = RedisLiveStore()
    try:
        await redis_live.connect()
        await redis_live.ping()
    except Exception:
        logger.exception("Redis unavailable — worker will persist history only")
        redis_live = None

    batch = BatchSettings.from_app_settings()
    logger.info(
        "ingest worker connecting exchange=%s queue=%s dlq=%s prefetch=%d batch_size=%d batch_max_age=%.2fs",
        settings.RABBITMQ_EXCHANGE,
        settings.RABBITMQ_QUEUE,
        settings.RABBITMQ_DLQ,
        settings.RABBITMQ_PREFETCH,
        batch.batch_size,
        batch.max_age_seconds,
    )

    connection = await aio_pika.connect_robust(
        url,
        client_properties={"connection_name": "volta-ingest-worker"},
    )
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=max(1, settings.RABBITMQ_PREFETCH))
    queue = await declare_topology(channel)

    batcher = IngestBatcher(
        write_many=timescale.upsert_many,
        tolerance_kw=settings.ENERGY_BALANCE_TOLERANCE_KW,
        batch=batch,
        live_publish=redis_live.set_and_publish if redis_live is not None else None,
    )
    batcher.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    lag_task = asyncio.create_task(
        QueueLagReporter(broker_fetch(connection, settings.RABBITMQ_QUEUE), settings.INGEST_LAG_POLL_SECONDS).run(stop),
        name="ingest-queue-lag",
    )

    consumer_tag = await queue.consume(batcher.submit)
    logger.info("ingest worker consuming %s (SIGTERM or Ctrl+C to stop)", queue.name)
    try:
        await stop.wait()
    finally:
        logger.info("ingest worker shutting down: no new messages, flushing the buffered batch")
        try:
            await queue.cancel(consumer_tag)
        except Exception:
            logger.warning("could not cancel the consumer cleanly", exc_info=True)
        # Deliveries already on their way are still handed to submit(); drain() flushes them too.
        clean = await batcher.drain(settings.INGEST_SHUTDOWN_TIMEOUT_SECONDS)
        logger.info("ingest worker drain %s", "complete" if clean else "incomplete (unflushed ticks were requeued)")
        lag_task.cancel()
        await asyncio.gather(lag_task, return_exceptions=True)
        await connection.close()
        await timescale.close()
        if redis_live is not None:
            await redis_live.close()
        shutdown_telemetry()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
