"""
Audit consumer for virtual device commands.

The simulator has its own queue and applies the command to the next tick.
This process records the result, the override, and the audit row.

Run from backend/:

    python -m app.workers.device_commands
"""
from __future__ import annotations

import asyncio
import json
import logging

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.core.metrics import start_worker_metrics_server
from app.core.telemetry import configure_telemetry, consumer_span, shutdown_telemetry
from app.modules.assistant.actions.execution import record_virtual_result

logger = logging.getLogger("volta.device_commands")


async def _on_message(message: AbstractIncomingMessage) -> None:
    with consumer_span(
        "device_command.process",
        message.headers,
        **{"messaging.system": "rabbitmq", "messaging.destination.name": settings.DEVICE_COMMAND_QUEUE},
    ):
        async with message.process(requeue=False):
            try:
                payload = json.loads(message.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("device command payload was not JSON")
                return
            if not isinstance(payload, dict):
                return
            async with AsyncSessionLocal() as session:
                result = await record_virtual_result(session, payload)
                await session.commit()
            logger.info(
                "device command result id=%s applied=%s status=%s",
                result.get("command_id"),
                result.get("applied"),
                result.get("status"),
            )


async def consume() -> None:
    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=10)
    exchange = await channel.declare_exchange(settings.DEVICE_COMMAND_EXCHANGE, ExchangeType.TOPIC, durable=True)
    queue = await channel.declare_queue(settings.DEVICE_COMMAND_QUEUE, durable=True)
    await queue.bind(exchange, routing_key=settings.DEVICE_COMMAND_ROUTING_KEY)
    await queue.consume(_on_message)
    logger.info("listening on %s", settings.DEVICE_COMMAND_QUEUE)
    await asyncio.Future()


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    configure_telemetry(settings, service_name="volta-device-worker")
    start_worker_metrics_server(settings.WORKER_METRICS_PORT)
    try:
        asyncio.run(consume())
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
