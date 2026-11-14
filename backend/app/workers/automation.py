"""
Automation worker: evaluates approved rules on live telemetry (the Monitor, tracker 4.4).

    python -m app.workers.automation

It reads its own copy of every tick (a second queue bound to the telemetry exchange), so it never slows ingest down and
scales on its own. The queue is short-lived by design: a tick is only useful for seconds, so messages expire and the
oldest are dropped if the worker is away, and a stopped worker cannot make the broker grow.

Run ONE replica per home set. A rule's state (pending, firing, cooling) lives in the worker, and two workers evaluating
the same home would both fire it. Scaling out means partitioning homes, which is Phase 5 work (tracker 5.2).
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from app.agents_platform.automation.executor import ExecutionResult, RuleExecutor
from app.agents_platform.automation.readings import reading_from_record
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.core.metrics import AUTOMATION_EVALUATIONS, start_worker_metrics_server
from app.core.telemetry import configure_telemetry, consumer_span, shutdown_telemetry
from app.modules.energy.schemas import TelemetryRecord

logger = logging.getLogger("volta.automation.worker")


@asynccontextmanager
async def _session_scope():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def process_payload(body: bytes, executor: RuleExecutor, scope=_session_scope) -> ExecutionResult | None:
    """
    Evaluate one telemetry message. Returns None for a message that is not a usable reading (dropped: a malformed
    tick must never stop the worker or be retried forever).
    """
    try:
        record = TelemetryRecord.model_validate(json.loads(body))
        reading = reading_from_record(record)
    except (ValueError, ValidationError, TypeError):
        logger.warning("automation worker dropped a message that is not a telemetry reading")
        return None
    names = {d.device_id: d.device_name.lower() for d in record.devices}
    try:
        async with scope() as db:
            return await executor.process(db, record.household_id, reading, record.timestamp, device_names=names)
    except Exception:  # noqa: BLE001 - one bad evaluation must not end the worker; the next tick is a fresh try
        AUTOMATION_EVALUATIONS.labels(outcome="error").inc()
        logger.exception("automation evaluation failed for %s", record.household_id)
        return None


async def declare_queue(channel: aio_pika.abc.AbstractChannel) -> aio_pika.abc.AbstractQueue:
    exchange = await channel.declare_exchange(settings.RABBITMQ_EXCHANGE, ExchangeType.TOPIC, durable=True)
    queue = await channel.declare_queue(
        settings.RABBITMQ_AUTOMATION_QUEUE,
        durable=True,
        arguments={
            "x-message-ttl": settings.RABBITMQ_AUTOMATION_TTL_MS,
            "x-max-length": settings.RABBITMQ_AUTOMATION_MAX_LENGTH,
            "x-overflow": "drop-head",
        },
    )
    await queue.bind(exchange, routing_key=settings.RABBITMQ_ROUTING_KEY)
    return queue


async def consume() -> None:
    from app.ai.config import ai_settings

    executor = RuleExecutor(cache_seconds=ai_settings.AI_AUTOMATION_CACHE_SECONDS)
    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=20)
    queue = await declare_queue(channel)

    async def on_message(message: AbstractIncomingMessage) -> None:
        with consumer_span("automation.evaluate", message.headers, **{"messaging.system": "rabbitmq"}):
            async with message.process(requeue=False):
                await process_payload(message.body, executor)

    await queue.consume(on_message)
    logger.info("automation worker listening on %s", settings.RABBITMQ_AUTOMATION_QUEUE)
    await asyncio.Future()


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    configure_telemetry(settings, service_name="volta-automation-worker")
    start_worker_metrics_server(settings.WORKER_METRICS_PORT)
    try:
        asyncio.run(consume())
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
