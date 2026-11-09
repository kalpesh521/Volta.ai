"""
Telemetry ingest worker.

Consumes RabbitMQ, validates JSON, writes TimescaleDB, updates Redis, then ACKs.
Poison payloads go to the DLQ. Timescale failures requeue. Redis failures are
logged; history is already durable so the message is still ACK'd.

Run from backend/:

    python -m app.workers.ingest
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.core.metrics import (
    INGEST_MESSAGE_AGE_SECONDS,
    INGEST_MESSAGES,
    INGEST_SECONDS,
    start_worker_metrics_server,
)
from app.core.telemetry import configure_telemetry, consumer_span, shutdown_telemetry
from app.modules.energy.pipeline import process_telemetry_body
from app.modules.energy.redis_live import RedisLiveStore
from app.modules.energy.timescale_store import TimescaleTelemetryStore

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
    """Process one tick inside a span that continues the producer's trace, when it sent one."""
    with consumer_span(
        "ingest.process",
        message.headers,
        **{"messaging.system": "rabbitmq", "messaging.destination.name": settings.RABBITMQ_QUEUE},
    ):
        with INGEST_SECONDS.time():
            await _process(
                message, tolerance_kw=tolerance_kw, timescale=timescale, redis_live=redis_live
            )


async def _process(
    message: AbstractIncomingMessage,
    *,
    tolerance_kw: float,
    timescale: TimescaleTelemetryStore,
    redis_live: RedisLiveStore | None,
) -> None:
    try:
        decision = process_telemetry_body(message.body, tolerance_kw)
        if decision.action == "reject" or decision.record is None:
            logger.warning("dead-letter telemetry: %s", decision.reason)
            INGEST_MESSAGES.labels(outcome="rejected").inc()
            await message.reject(requeue=False)
            return

        record = decision.record
        await timescale.upsert(record)
        if redis_live is not None:
            try:
                await redis_live.set_and_publish(record)
            except Exception:
                logger.exception(
                    "redis live update failed household=%s (tick stored in Timescale)",
                    record.household_id,
                )

        await message.ack()
        INGEST_MESSAGES.labels(outcome="stored").inc()
        INGEST_MESSAGE_AGE_SECONDS.observe(_age_seconds(record.timestamp))
        if record.energy_balance_status != "valid":
            logger.warning(
                "ingested household=%s ts=%s with energy-balance warning error_kw=%s",
                record.household_id,
                record.timestamp.isoformat(),
                record.energy_balance_error_kw,
            )
        else:
            logger.info(
                "ingested household=%s ts=%s solar_kw=%s",
                record.household_id,
                record.timestamp.isoformat(),
                record.solar_power_kw,
            )
    except Exception:
        logger.exception("ingest failed, requeue")
        INGEST_MESSAGES.labels(outcome="requeued").inc()
        try:
            await message.nack(requeue=True)
        except Exception:
            pass


def _age_seconds(timestamp: datetime) -> float:
    """Seconds between the tick's own timestamp and now. Never negative."""
    stamp = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - stamp.timestamp())


async def run() -> None:
    url = (settings.RABBITMQ_URL or "").strip()
    if not url:
        raise RuntimeError("RABBITMQ_URL is empty")

    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    configure_telemetry(settings, service_name="volta-ingest-worker")
    start_worker_metrics_server(settings.WORKER_METRICS_PORT)

    timescale = TimescaleTelemetryStore()
    await timescale.connect()
    await timescale.ensure_schema()

    redis_live: RedisLiveStore | None = RedisLiveStore()
    try:
        await redis_live.connect()
        await redis_live.ping()
    except Exception:
        logger.exception("Redis unavailable — worker will persist history only")
        redis_live = None

    logger.info(
        "ingest worker connecting exchange=%s queue=%s dlq=%s",
        settings.RABBITMQ_EXCHANGE,
        settings.RABBITMQ_QUEUE,
        settings.RABBITMQ_DLQ,
    )

    connection = await aio_pika.connect_robust(
        url,
        client_properties={"connection_name": "volta-ingest-worker"},
    )
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=max(1, settings.RABBITMQ_PREFETCH))
    queue = await declare_topology(channel)
    logger.info("ingest worker consuming %s (Ctrl+C to stop)", queue.name)

    stop = asyncio.Event()

    def _request_stop() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    async def _on_message(message: AbstractIncomingMessage) -> None:
        await handle_message(
            message,
            tolerance_kw=settings.ENERGY_BALANCE_TOLERANCE_KW,
            timescale=timescale,
            redis_live=redis_live,
        )

    consumer_tag = await queue.consume(_on_message)
    try:
        await stop.wait()
    finally:
        logger.info("ingest worker shutting down")
        await queue.cancel(consumer_tag)
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
