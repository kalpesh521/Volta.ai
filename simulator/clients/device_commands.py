"""Consume virtual-device commands without blocking telemetry generation."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aio_pika
from aio_pika import ExchangeType

logger = logging.getLogger("suryaa.device_commands")


def command_deadline(payload: dict[str, Any], now: datetime) -> datetime | None:
    minutes = payload.get("duration_minutes")
    if minutes is None:
        return None
    try:
        span = int(minutes)
    except (TypeError, ValueError):
        return None
    if span <= 0:
        return None
    issued = payload.get("issued_at")
    start = now
    if isinstance(issued, str):
        try:
            start = datetime.fromisoformat(issued)
        except ValueError:
            start = now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(minutes=span)


class DeviceCommandListener:
    """Best-effort consumer. A broker outage must not stop the simulator."""

    def __init__(self, url: str, exchange: str, queue: str, routing_key: str) -> None:
        self.url = url.strip()
        self.exchange_name = exchange.strip() or "device.commands"
        self.queue_name = queue.strip() or "simulator.device.commands"
        self.routing_key = routing_key.strip() or "device.command"
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None

    async def poll(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.url:
            return []
        try:
            queue = await self._ensure_queue()
        except Exception as exc:
            logger.warning("device command listener unavailable: %s", exc)
            self._queue = None
            return []
        found: list[dict[str, Any]] = []
        while len(found) < limit:
            try:
                incoming = await queue.get(timeout=0.05, fail=False)
            except Exception as exc:
                logger.warning("device command poll failed: %s", exc)
                self._queue = None
                break
            if incoming is None:
                break
            try:
                payload = json.loads(incoming.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await incoming.reject(requeue=False)
                continue
            if isinstance(payload, dict):
                found.append(payload)
            await incoming.ack()
        return found

    async def aclose(self) -> None:
        self._queue = None
        connection = self._connection
        self._connection = None
        if connection is not None and not connection.is_closed:
            await connection.close()

    async def _ensure_queue(self) -> aio_pika.abc.AbstractQueue:
        if self._queue is not None and self._connection is not None and not self._connection.is_closed:
            return self._queue
        self._connection = await aio_pika.connect_robust(self.url, timeout=3)
        channel = await self._connection.channel()
        exchange = await channel.declare_exchange(self.exchange_name, ExchangeType.TOPIC, durable=True)
        queue = await channel.declare_queue(self.queue_name, durable=True)
        await queue.bind(exchange, routing_key=self.routing_key)
        self._queue = queue
        return queue
