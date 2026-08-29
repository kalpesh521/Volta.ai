"""Publish simulator ticks to RabbitMQ. No Redis, TimescaleDB, or HTTP."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message

from simulator.models import TelemetryRecord

logger = logging.getLogger("suryaa.rabbitmq")

CONTENT_TYPE = "application/json"
APP_ID = "suryaa-simulator"


def build_message(record: TelemetryRecord) -> Message:
    """Persistent JSON body matching TelemetryRecord. Worker validates on consume."""
    ts = record.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Message(
        body=record.model_dump_json().encode("utf-8"),
        content_type=CONTENT_TYPE,
        content_encoding="utf-8",
        delivery_mode=DeliveryMode.PERSISTENT,
        timestamp=ts,
        app_id=APP_ID,
        type="telemetry.ingest",
        headers={
            "household_id": record.household_id,
            "data_source": record.data_source,
        },
    )


class RabbitMQPublisher:
    """
    Best-effort AMQP publisher. Generator ticks must not die if the broker is down.
    Declares the durable topic exchange only. The backend worker owns the queue,
    binding, and dead-letter topology.
    """

    def __init__(
        self,
        url: str,
        exchange: str = "telemetry",
        routing_key: str = "telemetry.ingest",
        timeout_seconds: float = 3.0,
    ) -> None:
        self.url = url.strip()
        self.exchange_name = exchange.strip() or "telemetry"
        self.routing_key = routing_key.strip() or "telemetry.ingest"
        self._timeout = timeout_seconds
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self._fail_count = 0

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(
            self.url,
            timeout=self._timeout,
            client_properties={"connection_name": APP_ID},
        )
        channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await channel.declare_exchange(
            self.exchange_name,
            ExchangeType.TOPIC,
            durable=True,
        )

    async def publish(self, record: TelemetryRecord) -> None:
        try:
            if self._exchange is None or self._connection is None or self._connection.is_closed:
                await self.connect()
            assert self._exchange is not None
            await self._exchange.publish(
                build_message(record),
                routing_key=self.routing_key,
                timeout=self._timeout,
            )
            if self._fail_count:
                logger.info("RabbitMQ publish recovered after %s failures", self._fail_count)
            self._fail_count = 0
        except Exception as exc:
            self._exchange = None
            self._note_failure(str(exc))

    def _note_failure(self, detail: str) -> None:
        self._fail_count += 1
        if self._fail_count <= 3 or self._fail_count % 50 == 0:
            logger.warning("rabbitmq publish failed (%s): %s", self._fail_count, detail)

    async def aclose(self) -> None:
        self._exchange = None
        connection = self._connection
        self._connection = None
        if connection is not None and not connection.is_closed:
            await connection.close()
