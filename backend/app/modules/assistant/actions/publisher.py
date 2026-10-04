"""Queue a device command. Publishing does not mean the appliance changed."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from app.core.config import settings

logger = logging.getLogger("volta.actions.publisher")


class CommandBrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceCommand:
    command_id: str
    household_id: str
    user_id: str
    device: str
    command: str


class CommandPublisher(Protocol):
    async def publish(self, command: DeviceCommand) -> str: ...


class RecordingPublisher:
    """Test double. Records the command and never opens a broker connection."""

    def __init__(self) -> None:
        self.commands: list[DeviceCommand] = []

    async def publish(self, command: DeviceCommand) -> str:
        self.commands.append(command)
        return command.command_id


class RabbitDevicePublisher:
    """Durable topic exchange. The worker owns applying the command."""

    def __init__(self, url: str, exchange: str, queue: str, routing_key: str) -> None:
        self._url = url
        self._exchange_name = exchange
        self._queue_name = queue
        self._routing_key = routing_key

    async def publish(self, command: DeviceCommand) -> str:
        try:
            connection = await aio_pika.connect_robust(self._url, timeout=3)
        except Exception as exc:
            raise CommandBrokerError("RabbitMQ is unavailable") from exc
        try:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(self._exchange_name, ExchangeType.TOPIC, durable=True)
            queue = await channel.declare_queue(self._queue_name, durable=True)
            await queue.bind(exchange, routing_key=self._routing_key)
            body = {
                "command_id": command.command_id,
                "household_id": command.household_id,
                "user_id": command.user_id,
                "device": command.device,
                "command": command.command,
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "applied": False,
            }
            await exchange.publish(
                Message(
                    body=json.dumps(body).encode("utf-8"),
                    content_type="application/json",
                    delivery_mode=DeliveryMode.PERSISTENT,
                    message_id=command.command_id,
                    type="device.command",
                ),
                routing_key=self._routing_key,
            )
        except CommandBrokerError:
            raise
        except Exception as exc:
            raise CommandBrokerError("The device command could not be queued") from exc
        finally:
            await connection.close()
        logger.info("device command queued id=%s household=%s device=%s", command.command_id, command.household_id, command.device)
        return command.command_id


def new_command_id() -> str:
    return uuid.uuid4().hex


def default_publisher() -> CommandPublisher:
    return RabbitDevicePublisher(
        settings.RABBITMQ_URL,
        settings.DEVICE_COMMAND_EXCHANGE,
        settings.DEVICE_COMMAND_QUEUE,
        settings.DEVICE_COMMAND_ROUTING_KEY,
    )
