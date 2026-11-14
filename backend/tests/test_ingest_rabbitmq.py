"""
Competing consumers against a real RabbitMQ. Opt-in:

    TEST_RABBITMQ_URL=amqp://volta:volta@127.0.0.1:5672/volta pytest tests/test_ingest_rabbitmq.py

Uses its own uniquely named exchange, queues and dead-letter queue (deleted afterwards), and an
in-memory fake for the database, so it touches nothing else on the broker.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import timedelta

import aio_pika
import pytest

from app.core.config import settings
from app.workers import ingest as ingest_worker
from app.workers.ingest_batch import BatchSettings, IngestBatcher
from tests.test_ingest_batch import BASE, FakeStore
from tests.test_ingest_worker import _tick

RABBITMQ_URL = os.environ.get("TEST_RABBITMQ_URL", "")
pytestmark = pytest.mark.skipif(not RABBITMQ_URL, reason="TEST_RABBITMQ_URL is not set")


def _body(household: str, minute: int) -> bytes:
    stamp = (BASE + timedelta(minutes=minute)).isoformat()
    return json.dumps(_tick(timestamp=stamp, household_id=household)).encode()


async def _wait_for(predicate, timeout: float = 15.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


class Replica:
    """What `ingest.run()` wires together for one process, with a fake database."""

    def __init__(
        self, connection, store: FakeStore, *, batch_size: int = 10, prefetch: int = 30, max_attempts: int = 5
    ) -> None:
        self.connection, self.store = connection, store
        self.batch_size, self.prefetch, self.max_attempts = batch_size, prefetch, max_attempts
        self.batcher: IngestBatcher | None = None
        self.consumer_tag: str | None = None
        self.queue = None

    async def start(self) -> None:
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=self.prefetch)
        self.queue = await ingest_worker.declare_topology(channel)
        self.batcher = IngestBatcher(
            write_many=self.store.write_many,
            tolerance_kw=0.05,
            batch=BatchSettings(
                batch_size=self.batch_size,
                max_age_seconds=0.1,
                max_buffer=self.prefetch,
                max_attempts=self.max_attempts,
                backoff_base_seconds=0.0,
            ),
        )
        self.batcher.start()
        self.consumer_tag = await self.queue.consume(self.batcher.submit)

    async def stop(self) -> bool:
        """The SIGTERM path of run(): stop consuming, then flush."""
        await self.queue.cancel(self.consumer_tag)
        return await self.batcher.drain(10)


async def _queue_state(connection, name: str) -> tuple[int, int]:
    channel = await connection.channel()
    queue = await channel.declare_queue(name, passive=True)
    return queue.declaration_result.message_count, queue.declaration_result.consumer_count


async def _publish(connection, exchange_name: str, bodies: list[bytes]) -> None:
    channel = await connection.channel(publisher_confirms=True)
    exchange = await channel.get_exchange(exchange_name)
    for body in bodies:
        await exchange.publish(
            aio_pika.Message(body=body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=settings.RABBITMQ_ROUTING_KEY,
        )


@pytest.fixture
def topology(monkeypatch):
    """Private names for the queue topology, restored and deleted afterwards."""
    suffix = uuid.uuid4().hex[:8]
    names = {
        "RABBITMQ_EXCHANGE": f"p5test.telemetry.{suffix}",
        "RABBITMQ_QUEUE": f"p5test.ingest.{suffix}",
        "RABBITMQ_DLX": f"p5test.dlx.{suffix}",
        "RABBITMQ_DLQ": f"p5test.ingest.dead.{suffix}",
    }
    for key, value in names.items():
        monkeypatch.setattr(settings, key, value)
    return names


async def _cleanup(connection, names: dict) -> None:
    channel = await connection.channel()
    for queue in (names["RABBITMQ_QUEUE"], names["RABBITMQ_DLQ"]):
        await channel.queue_delete(queue)
    for exchange in (names["RABBITMQ_EXCHANGE"], names["RABBITMQ_DLX"]):
        await channel.exchange_delete(exchange)


async def test_two_replicas_share_one_queue_and_every_distinct_tick_is_stored(topology):
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        bootstrap = await connection.channel()
        await ingest_worker.declare_topology(bootstrap)

        store = FakeStore()  # one "database" behind both replicas
        replicas = [Replica(connection, store, prefetch=20), Replica(connection, store, prefetch=20)]
        for replica in replicas:
            await replica.start()

        # 120 distinct ticks, the first 20 published twice (a retrying producer).
        bodies = [_body("p5-home", minute) for minute in range(120)] + [_body("p5-home", m) for m in range(20)]
        await _publish(connection, topology["RABBITMQ_EXCHANGE"], bodies)
        await _wait_for(lambda: len(store.rows) == 120)
        await _wait_for(lambda: sum(r.batcher.stats.stored for r in replicas) == 140)

        ready, consumers = await _queue_state(connection, topology["RABBITMQ_QUEUE"])
        assert ready == 0 and consumers == 2
        assert len(store.rows) == 120
        # How the broker splits deliveries between the two is its business and varies run to run.
        for replica in replicas:
            assert await replica.stop() is True
    finally:
        await _cleanup(connection, topology)
        await connection.close()


async def test_a_replica_stopped_mid_stream_loses_nothing(topology):
    """Stop one replica while messages are flowing: what it had buffered is flushed, the rest stays queued."""
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        bootstrap = await connection.channel()
        await ingest_worker.declare_topology(bootstrap)
        total = 300
        await _publish(connection, topology["RABBITMQ_EXCHANGE"], [_body("p5-home", m) for m in range(total)])

        store = FakeStore()
        first = Replica(connection, store, batch_size=50, prefetch=100)
        await first.start()
        await _wait_for(lambda: len(store.rows) >= 50)
        assert await first.stop() is True  # SIGTERM while there are still messages queued

        # Nothing vanished: every tick is either stored or still waiting. Requeues and the last acks may
        # still be in flight at the broker for a moment, so poll until the counts settle.
        deadline = asyncio.get_running_loop().time() + 10
        while True:
            ready, _ = await _queue_state(connection, topology["RABBITMQ_QUEUE"])
            if ready + len(store.rows) == total:
                break
            assert asyncio.get_running_loop().time() < deadline, f"lost messages: {ready} queued, {len(store.rows)} stored"
            await asyncio.sleep(0.1)

        # A replacement replica picks up the rest.
        second = Replica(connection, store, batch_size=50, prefetch=100)
        await second.start()
        await _wait_for(lambda: len(store.rows) == total)
        assert await second.stop() is True
    finally:
        await _cleanup(connection, topology)
        await connection.close()


async def test_a_poison_message_and_a_permanently_failing_tick_reach_the_dead_letter_queue(topology):
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    try:
        bootstrap = await connection.channel()
        await ingest_worker.declare_topology(bootstrap)
        await _publish(
            connection,
            topology["RABBITMQ_EXCHANGE"],
            [b"not json", _body("p5-bad", 0), _body("p5-home", 1), _body("p5-home", 2)],
        )
        store = FakeStore()
        store.bad_households = {"p5-bad"}
        replica = Replica(connection, store, batch_size=4, max_attempts=2)
        await replica.start()
        await _wait_for(lambda: replica.batcher.stats.dead_lettered == 1 and len(store.rows) == 2)

        dead_ready, _ = await _queue_state(connection, topology["RABBITMQ_DLQ"])
        assert dead_ready == 2  # the unparseable body and the tick that kept failing
        assert await replica.stop() is True
    finally:
        await _cleanup(connection, topology)
        await connection.close()
