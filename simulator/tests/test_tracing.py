"""The simulator starts the trace: a W3C `traceparent` per telemetry message (Phase 5.5)."""

from __future__ import annotations

import httpx

from simulator.clients.ingest import IngestClient
from simulator.clients.rabbitmq import RabbitMQPublisher, build_message
from simulator.config import SimulatorConfig
from simulator.tracing import is_valid_traceparent, new_traceparent, trace_headers
from tests.test_simulator import _telemetry_record


def test_a_traceparent_has_the_w3c_shape_and_never_an_all_zero_id():
    for _ in range(200):
        value = new_traceparent()
        assert is_valid_traceparent(value)
        version, trace_id, span_id, flags = value.split("-")
        assert version == "00" and len(trace_id) == 32 and len(span_id) == 16 and flags in {"00", "01"}


def test_validator_rejects_malformed_and_zero_ids():
    assert not is_valid_traceparent("")
    assert not is_valid_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01")
    assert not is_valid_traceparent("00-" + "a" * 32 + "-" + "0" * 16 + "-01")
    assert not is_valid_traceparent("00-" + "A" * 32 + "-" + "1" * 16 + "-01")  # lower case only
    assert not is_valid_traceparent("01-" + "a" * 32 + "-" + "1" * 16 + "-01")  # only version 00 is produced here
    assert not is_valid_traceparent("00-" + "a" * 31 + "-" + "1" * 16 + "-01")


def test_every_call_is_a_new_trace():
    assert len({new_traceparent() for _ in range(500)}) == 500


def test_the_sample_ratio_controls_the_sampled_flag():
    assert all(new_traceparent(1.0).endswith("-01") for _ in range(50))
    assert all(new_traceparent(0.0).endswith("-00") for _ in range(50))
    assert all(new_traceparent(7.0).endswith("-01") for _ in range(20))  # clamped to 1
    assert all(new_traceparent(-1.0).endswith("-00") for _ in range(20))  # clamped to 0
    assert new_traceparent(0.5, rng=lambda: 0.49).endswith("-01")
    assert new_traceparent(0.5, rng=lambda: 0.50).endswith("-00")


def test_the_request_id_is_the_trace_id():
    headers = trace_headers()
    assert headers["x-request-id"] == headers["traceparent"].split("-")[1]


def test_a_telemetry_message_carries_the_trace_headers_next_to_the_existing_ones():
    message = build_message(_telemetry_record(), trace_sample_ratio=1.0)
    assert is_valid_traceparent(message.headers["traceparent"])
    assert message.headers["x-request-id"] == message.headers["traceparent"].split("-")[1]
    assert message.headers["household_id"] == "home_001"  # unchanged


def test_without_a_ratio_no_trace_headers_are_sent():
    message = build_message(_telemetry_record())
    assert "traceparent" not in message.headers and "x-request-id" not in message.headers


class _RecordingExchange:
    def __init__(self) -> None:
        self.sent = []

    async def publish(self, message, routing_key, timeout=None):
        self.sent.append(message)


class _OpenConnection:
    is_closed = False


async def test_each_published_tick_starts_its_own_trace():
    publisher = RabbitMQPublisher("amqp://unused", trace_sample_ratio=1.0)
    exchange = _RecordingExchange()
    publisher._exchange, publisher._connection = exchange, _OpenConnection()

    for _ in range(3):
        await publisher.publish(_telemetry_record())

    trace_ids = [m.headers["traceparent"].split("-")[1] for m in exchange.sent]
    assert len(trace_ids) == 3 and len(set(trace_ids)) == 3


async def test_the_http_ingest_client_sends_a_traceparent_too():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    client = IngestClient("http://backend:8000", "token", trace_sample_ratio=1.0)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.publish(_telemetry_record())
    await client.publish(_telemetry_record())

    assert all(is_valid_traceparent(r.headers["traceparent"]) for r in seen)
    assert seen[0].headers["traceparent"] != seen[1].headers["traceparent"]
    assert seen[0].headers["x-ingest-token"] == "token"

    quiet = IngestClient("http://backend:8000", "token")
    quiet._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await quiet.publish(_telemetry_record())
    assert "traceparent" not in seen[-1].headers


def test_propagation_is_on_by_default_and_configurable(monkeypatch):
    assert SimulatorConfig().trace_propagation_enabled is True
    assert SimulatorConfig().trace_sample_ratio == 1.0
    monkeypatch.setenv("TRACE_PROPAGATION_ENABLED", "false")
    monkeypatch.setenv("TRACE_SAMPLE_RATIO", "0.25")
    config = SimulatorConfig()
    assert config.trace_propagation_enabled is False and config.trace_sample_ratio == 0.25
