"""Batched, horizontally scaled ingest (Phase 5.1). Fakes only: no broker, no database."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from app.workers.ingest_batch import BatchSettings, IngestBatcher, is_transient_db_error
from app.workers.queue_lag import QueueLagReporter
from tests.test_ingest_worker import TZ, _tick

TOLERANCE = 0.05
BASE = datetime(2026, 8, 29, 12, 0, tzinfo=TZ)


def _body(minute: int = 0, household: str = "home_001", **overrides) -> bytes:
    stamp = BASE + timedelta(minutes=minute)
    return json.dumps(_tick(timestamp=stamp.isoformat(), household_id=household, **overrides)).encode()


DOUBLE_SETTLED: list[str] = []  # the real broker closes the channel on a second ack/nack: never allowed


@pytest.fixture(autouse=True)
def _no_message_is_settled_twice():
    DOUBLE_SETTLED.clear()
    yield
    assert DOUBLE_SETTLED == [], f"settled twice: {DOUBLE_SETTLED}"


class FakeMessage:
    def __init__(self, body: bytes, log: list, name: str, headers: dict | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.name = name
        self.outcome: str | None = None
        self._log = log

    def _once(self) -> None:
        if self.outcome is not None:
            DOUBLE_SETTLED.append(self.name)

    async def ack(self) -> None:
        self._once()
        self.outcome = "ack"
        self._log.append(("ack", self.name))

    async def nack(self, requeue: bool = True) -> None:
        self._once()
        self.outcome = "requeue" if requeue else "drop"
        self._log.append(("nack", self.name))

    async def reject(self, requeue: bool = False) -> None:
        self._once()
        self.outcome = "dead" if not requeue else "requeue"
        self._log.append(("reject", self.name))


class FakeStore:
    """Records every write in order and can be told how to fail. Rows are keyed like the real table."""

    def __init__(self, log: list | None = None) -> None:
        self.log = log if log is not None else []
        self.rows: dict[tuple, object] = {}
        self.calls: list[list] = []
        self.error: Exception | None = None
        self.bad_households: set[str] = set()
        self.gate: asyncio.Event | None = None

    async def write_many(self, records) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if any(record.household_id in self.bad_households for record in records):
            raise ValueError("value out of range")
        self.calls.append([record.household_id for record in records])
        self.log.append(("write", len(records)))
        for record in records:
            self.rows[(record.household_id, record.timestamp)] = record  # upsert on the primary key


def _batcher(store: FakeStore, **cfg) -> IngestBatcher:
    defaults = dict(batch_size=3, max_age_seconds=5.0, max_buffer=10, backoff_base_seconds=0.0)
    defaults.update(cfg)
    live = defaults.pop("live", None)
    sleep = defaults.pop("sleep", None)
    kwargs = {"live_publish": live}
    if sleep is not None:
        kwargs["sleep"] = sleep
    batcher = IngestBatcher(
        write_many=store.write_many,
        tolerance_kw=TOLERANCE,
        batch=BatchSettings(**defaults),
        **kwargs,
    )
    batcher.start()
    return batcher


async def _settled(*messages: FakeMessage, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while any(m.outcome is None for m in messages):
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


def _count(outcome: str) -> float:
    return REGISTRY.get_sample_value("volta_ingest_messages_total", {"outcome": outcome}) or 0.0


# --- batching boundaries -------------------------------------------------------------------


async def test_a_full_batch_flushes_at_once_without_waiting_for_the_age_limit():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=3, max_age_seconds=60.0)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(3)]
    for message in messages:
        await batcher.submit(message)

    await _settled(*messages)

    assert store.calls == [["home_001"] * 3]  # one write for all three
    assert [m.outcome for m in messages] == ["ack"] * 3
    await batcher.drain(1)


async def test_a_partial_batch_flushes_when_the_oldest_tick_reaches_the_age_limit():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=100, max_age_seconds=0.05)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    assert store.calls == []  # not yet: below size and younger than the limit

    await _settled(*messages)

    assert len(store.calls) == 1 and len(store.calls[0]) == 2
    await batcher.drain(1)


async def test_more_ticks_than_one_batch_are_written_in_batch_sized_chunks():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=3, max_age_seconds=0.05)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(7)]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)
    assert sorted(len(call) for call in store.calls) == [1, 3, 3]
    await batcher.drain(1)


async def test_memory_is_bounded_a_full_buffer_makes_submit_wait():
    store, log = FakeStore(), []
    store.gate = asyncio.Event()  # the write hangs, so nothing leaves the buffer once the first batch is taken
    batcher = _batcher(store, batch_size=2, max_buffer=2, max_age_seconds=0.01)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(6)]
    tasks = [asyncio.create_task(batcher.submit(m)) for m in messages]
    await asyncio.sleep(0.15)

    # 2 in the batch being written + 2 in the buffer = 4 accepted. The other 2 submits are still waiting.
    assert batcher.buffered == 4
    assert sum(t.done() for t in tasks) == 4

    store.gate.set()
    await asyncio.gather(*tasks)
    await _settled(*messages)
    assert batcher.buffered == 0
    await batcher.drain(1)


# --- ack ordering ----------------------------------------------------------------------------


async def test_no_message_is_acked_before_its_batch_is_committed():
    log: list = []
    store = FakeStore(log)
    batcher = _batcher(store, batch_size=3, max_age_seconds=60.0)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(3)]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)

    assert log[0] == ("write", 3)
    assert [entry[0] for entry in log[1:]] == ["ack"] * 3
    await batcher.drain(1)


async def test_nothing_is_acked_when_the_write_fails():
    store, log = FakeStore(), []
    store.error = ConnectionError("db down")
    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)

    assert [m.outcome for m in messages] == ["requeue", "requeue"]
    assert not any(entry[0] == "ack" for entry in log)
    await batcher.drain(1)


# --- failure paths ---------------------------------------------------------------------------


async def test_a_transient_failure_requeues_the_whole_batch_without_counting_an_attempt_then_recovers():
    store, log = FakeStore(), []
    store.error = ConnectionError("db down")
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0, backoff_base_seconds=1.0, backoff_max_seconds=3.0, sleep=fake_sleep)
    first = [FakeMessage(_body(i), log, f"a{i}") for i in range(2)]
    for message in first:
        await batcher.submit(message)
    await _settled(*first)
    assert delays == [1.0]

    # Redelivered while the database is still down: the pause doubles, up to the cap.
    again = [FakeMessage(_body(i), log, f"b{i}") for i in range(2)]
    for message in again:
        await batcher.submit(message)
    await _settled(*again)
    third = [FakeMessage(_body(i), log, f"c{i}") for i in range(2)]
    for message in third:
        await batcher.submit(message)
    await _settled(*third)
    assert delays == [1.0, 2.0, 3.0]
    assert batcher.stats.dead_lettered == 0  # an outage never dead-letters data

    store.error = None
    final = [FakeMessage(_body(i), log, f"d{i}") for i in range(2)]
    for message in final:
        await batcher.submit(message)
    await _settled(*final)
    assert [m.outcome for m in final] == ["ack", "ack"]
    await batcher.drain(1)


async def test_a_write_that_hangs_past_the_timeout_counts_as_a_transient_failure():
    store, log = FakeStore(), []
    store.gate = asyncio.Event()  # never set
    batcher = _batcher(store, batch_size=1, write_timeout_seconds=0.1)
    message = FakeMessage(_body(), log, "m")
    await batcher.submit(message)
    await _settled(message)
    assert message.outcome == "requeue"
    await batcher.drain(1)


async def test_one_bad_tick_does_not_hold_back_the_good_ones_and_is_dead_lettered_after_the_retry_limit():
    store, log = FakeStore(), []
    store.bad_households = {"home_bad"}
    batcher = _batcher(store, batch_size=3, max_age_seconds=0.05, max_attempts=3)

    good = [FakeMessage(_body(1), log, "g1"), FakeMessage(_body(2), log, "g2")]
    bad = FakeMessage(_body(3, household="home_bad"), log, "bad-1")
    for message in (*good, bad):
        await batcher.submit(message)
    await _settled(*good, bad)

    assert [m.outcome for m in good] == ["ack", "ack"]
    assert bad.outcome == "requeue"  # attempt 1 of 3
    assert ("home_001", BASE + timedelta(minutes=1)) in store.rows

    # The broker redelivers it (attempt 2, then 3 -> dead letter). Each redelivery is a new delivery object.
    second = FakeMessage(_body(3, household="home_bad"), log, "bad-2")
    third = FakeMessage(_body(3, household="home_bad"), log, "bad-3")
    await batcher.submit(second)
    await _settled(second)
    assert second.outcome == "requeue"
    await batcher.submit(third)
    await _settled(third)
    assert third.outcome == "dead"  # reject(requeue=False) -> DLQ
    assert batcher.stats.dead_lettered == 1
    await batcher.drain(1)


async def test_the_brokers_delivery_count_counts_toward_the_retry_limit_on_a_fresh_replica():
    store, log = FakeStore(), []
    store.bad_households = {"home_bad"}
    batcher = _batcher(store, batch_size=1, max_attempts=3)
    message = FakeMessage(_body(0, household="home_bad"), log, "m", headers={"x-delivery-count": 2})
    await batcher.submit(message)
    await _settled(message)
    assert message.outcome == "dead"
    await batcher.drain(1)


async def test_a_poison_payload_is_dead_lettered_at_once_and_does_not_disturb_the_batch():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0)
    before = _count("rejected")
    poison = FakeMessage(b"not json", log, "poison")
    await batcher.submit(poison)
    assert poison.outcome == "dead" and _count("rejected") == before + 1

    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)
    assert [m.outcome for m in messages] == ["ack", "ack"]
    await batcher.drain(1)


async def test_a_failing_ack_after_commit_does_not_break_the_batch_and_the_rewrite_is_idempotent():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0)

    class AckFails(FakeMessage):
        async def ack(self) -> None:
            raise RuntimeError("channel closed")

    first = AckFails(_body(0), log, "first")
    second = FakeMessage(_body(1), log, "second")
    await batcher.submit(first)
    await batcher.submit(second)
    await _settled(second)
    assert second.outcome == "ack" and len(store.rows) == 2

    # The broker redelivers the first tick (its ack was lost): same primary key, still one row.
    redelivered = FakeMessage(_body(0), log, "first-again")
    await batcher.submit(redelivered)
    await batcher.drain(2)
    assert redelivered.outcome == "ack" and len(store.rows) == 2


# --- duplicate safety ------------------------------------------------------------------------


async def test_a_duplicate_inside_one_batch_is_written_once_and_both_deliveries_are_acked():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=3, max_age_seconds=60.0)
    before = _count("deduplicated")
    messages = [FakeMessage(_body(0), log, "a"), FakeMessage(_body(0), log, "a-dup"), FakeMessage(_body(1), log, "b")]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)

    assert store.calls == [["home_001", "home_001"]]  # 2 distinct ticks, not 3 rows
    assert [m.outcome for m in messages] == ["ack"] * 3
    assert _count("deduplicated") == before + 1
    await batcher.drain(1)


async def test_two_replicas_on_one_queue_never_double_a_row_even_when_both_receive_the_same_tick():
    store, log = FakeStore(), []
    replica_a = _batcher(store, batch_size=2, max_age_seconds=0.02)
    replica_b = _batcher(store, batch_size=2, max_age_seconds=0.02)
    shared = [FakeMessage(_body(0), log, "a-0"), FakeMessage(_body(0), log, "b-0")]  # redelivered to the other replica
    await replica_a.submit(shared[0])
    await replica_b.submit(shared[1])
    await _settled(*shared)

    assert len(store.rows) == 1
    assert [m.outcome for m in shared] == ["ack", "ack"]
    await replica_a.drain(1)
    await replica_b.drain(1)


# --- graceful shutdown -----------------------------------------------------------------------


async def test_shutdown_flushes_a_partial_batch_and_acks_it_after_the_commit():
    log: list = []
    store = FakeStore(log)
    batcher = _batcher(store, batch_size=100, max_age_seconds=60.0)  # would not flush for a minute
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    assert store.calls == []

    clean = await batcher.drain(timeout=2)

    assert clean is True
    assert [m.outcome for m in messages] == ["ack", "ack"]
    assert log[0] == ("write", 2)  # committed first, acked after
    late = FakeMessage(_body(9), log, "late")
    await batcher.submit(late)
    assert late.outcome == "requeue"  # after shutdown began: left for another replica


async def test_shutdown_that_cannot_finish_in_time_requeues_everything_unsettled_and_never_double_settles():
    store, log = FakeStore(), []
    store.gate = asyncio.Event()  # the write never finishes
    batcher = _batcher(store, batch_size=2, max_age_seconds=0.01, write_timeout_seconds=60)
    in_flight = [FakeMessage(_body(i), log, f"f{i}") for i in range(2)]
    for message in in_flight:
        await batcher.submit(message)
    await asyncio.sleep(0.05)  # batch is now being written
    buffered = FakeMessage(_body(5), log, "buffered")
    await batcher.submit(buffered)

    clean = await batcher.drain(timeout=0.1)

    assert clean is False
    assert {m.outcome for m in (*in_flight, buffered)} == {"requeue"}


async def test_live_snapshot_is_published_after_the_ack_and_a_redis_failure_changes_nothing():
    log: list = []
    store = FakeStore(log)

    async def live(record) -> None:
        log.append(("live", record.timestamp))
        raise RuntimeError("redis down")

    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0, live=live)
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    await batcher.drain(2)

    kinds = [entry[0] for entry in log]
    assert kinds == ["write", "ack", "ack", "live", "live"]
    assert [m.outcome for m in messages] == ["ack", "ack"]


async def test_a_hung_redis_cannot_hold_an_ack():
    log: list = []
    store = FakeStore(log)

    async def live(record) -> None:
        await asyncio.sleep(30)

    batcher = _batcher(store, batch_size=1, max_age_seconds=60.0, live=live, redis_timeout_seconds=0.05)
    message = FakeMessage(_body(), log, "m")
    await batcher.submit(message)
    await _settled(message)
    assert message.outcome == "ack"
    await batcher.drain(2)


# --- settings, errors, lag -------------------------------------------------------------------


def test_batch_size_is_clamped_to_the_prefetch(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RABBITMQ_PREFETCH", 10)
    monkeypatch.setattr(settings, "INGEST_BATCH_SIZE", 500)
    cfg = BatchSettings.from_app_settings()
    assert cfg.batch_size == 10 and cfg.max_buffer == 10

    monkeypatch.setattr(settings, "INGEST_BATCH_SIZE", 0)
    assert BatchSettings.from_app_settings().batch_size == 1


def test_the_defaults_are_safe_and_batch_no_larger_than_half_the_prefetch():
    from app.core.config import settings

    assert 1 <= settings.INGEST_BATCH_SIZE <= settings.RABBITMQ_PREFETCH // 2
    assert settings.INGEST_SHUTDOWN_TIMEOUT_SECONDS < 30  # under the default Kubernetes grace period
    assert settings.INGEST_MAX_ATTEMPTS >= 1


def test_which_database_errors_are_transient():
    class Pg(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    assert is_transient_db_error(ConnectionRefusedError())
    assert is_transient_db_error(asyncio.TimeoutError())
    assert is_transient_db_error(Pg("57P01"))  # administrator shutdown
    assert is_transient_db_error(Pg("40P01"))  # deadlock
    assert is_transient_db_error(Pg("53300"))  # too many connections
    assert not is_transient_db_error(Pg("22003"))  # numeric value out of range: the data, not the server
    assert not is_transient_db_error(Pg("23502"))  # not-null violation
    assert not is_transient_db_error(ValueError("anything unrecognised"))


async def test_queue_lag_gauges_follow_the_broker_and_keep_the_last_value_on_failure():
    answers = iter([(120, 3), RuntimeError("broker gone"), (7, 3)])

    async def fetch():
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    reporter = QueueLagReporter(fetch, interval_seconds=1)
    errors_before = REGISTRY.get_sample_value("volta_ingest_lag_poll_errors_total") or 0.0

    assert await reporter.poll_once() is True
    assert REGISTRY.get_sample_value("volta_ingest_queue_depth") == 120
    assert REGISTRY.get_sample_value("volta_ingest_queue_consumers") == 3

    assert await reporter.poll_once() is False
    assert REGISTRY.get_sample_value("volta_ingest_queue_depth") == 120  # stale, not a false zero
    assert REGISTRY.get_sample_value("volta_ingest_lag_poll_errors_total") == errors_before + 1

    assert await reporter.poll_once() is True
    assert REGISTRY.get_sample_value("volta_ingest_queue_depth") == 7


async def test_batch_metrics_are_recorded():
    store, log = FakeStore(), []
    batcher = _batcher(store, batch_size=2, max_age_seconds=60.0)
    before = REGISTRY.get_sample_value("volta_ingest_flushes_total", {"outcome": "committed"}) or 0.0
    count_before = REGISTRY.get_sample_value("volta_ingest_batch_size_count") or 0.0
    messages = [FakeMessage(_body(i), log, f"m{i}") for i in range(2)]
    for message in messages:
        await batcher.submit(message)
    await _settled(*messages)
    assert REGISTRY.get_sample_value("volta_ingest_flushes_total", {"outcome": "committed"}) == before + 1
    assert REGISTRY.get_sample_value("volta_ingest_batch_size_count") == count_before + 1
    await batcher.drain(1)
