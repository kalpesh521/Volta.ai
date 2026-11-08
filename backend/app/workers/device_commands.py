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
from app.modules.assistant.actions.execution import record_virtual_result

logger = logging.getLogger("volta.device_commands")


async def _on_message(message: AbstractIncomingMessage) -> None:
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(consume())


if __name__ == "__main__":
    main()
