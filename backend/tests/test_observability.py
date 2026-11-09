"""Request ids, log correlation, trace propagation over the queue, and metrics."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.core.logging_config import CorrelationFilter, JsonFormatter
from app.core.metrics import metrics_endpoint, track_ws_connection
from app.core.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    get_request_id,
    headers_with_request_id,
    request_id_scope,
    sanitize_request_id,
)
from app.core.telemetry import configure_telemetry, consumer_span, get_tracer, inject_trace_headers
from app.workers.ingest import handle_message
from tests.assistant_helpers import FakeStructuredLLM, auth_headers
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_ingest_worker import _tick


@pytest.fixture(scope="module")
def spans() -> InMemorySpanExporter:
    """Install one real tracer provider for this process and record what it finishes."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture(autouse=True)
def _clear_spans(spans):
    spans.clear()


# --- request ids ----------------------------------------------------------------------


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/id")
    async def read_id() -> dict:
        return {"seen_by_handler": get_request_id()}

    return app


async def _get_id(headers: dict[str, str] | None = None):
    async with AsyncClient(transport=ASGITransport(app=_probe_app()), base_url="http://t") as client:
        return await client.get("/id", headers=headers)


async def test_a_request_id_is_generated_and_echoed():
    response = await _get_id()

    echoed = response.headers[REQUEST_ID_HEADER]
    assert len(echoed) == 32
    assert response.json()["seen_by_handler"] == echoed


async def test_a_well_formed_incoming_request_id_is_kept():
    response = await _get_id({REQUEST_ID_HEADER: "client-req_42.a"})

    assert response.headers[REQUEST_ID_HEADER] == "client-req_42.a"
    assert response.json()["seen_by_handler"] == "client-req_42.a"


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "semi;colon", "a/b", "<script>"])
async def test_a_malformed_incoming_request_id_is_replaced(bad):
    response = await _get_id({REQUEST_ID_HEADER: bad})

    assert response.headers[REQUEST_ID_HEADER] != bad
    assert len(response.headers[REQUEST_ID_HEADER]) == 32


async def test_the_request_id_does_not_leak_after_the_request():
    await _get_id({REQUEST_ID_HEADER: "first"})

    assert get_request_id() is None


def test_sanitize_never_returns_an_unsafe_value():
    assert sanitize_request_id("ok-1") == "ok-1"
    assert sanitize_request_id(None) != sanitize_request_id(None)  # fresh ids
    assert sanitize_request_id("a b") != "a b"


async def test_the_real_app_returns_a_request_id(client):
    response = await client.get("/health", headers={REQUEST_ID_HEADER: "trace-me-1"})

    assert response.headers[REQUEST_ID_HEADER] == "trace-me-1"


async def test_the_assistant_response_carries_the_same_request_id(client):
    from app.modules.assistant.deps import get_structured_llm
    from main import app

    app.dependency_overrides[get_structured_llm] = lambda: FakeStructuredLLM()
    headers = await auth_headers(client, "obs-rid@example.com")
    await complete_hybrid_onboarding(client, headers)

    response = await client.post(
        "/assistant/me/ask",
        json={"question": "What's the capital of France?"},
        headers={**headers, REQUEST_ID_HEADER: "obs-req-7"},
    )

    assert response.headers[REQUEST_ID_HEADER] == "obs-req-7"
    assert response.json()["meta"]["request_id"] == "obs-req-7"


# --- log correlation ---------------------------------------------------------------------


def _record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("volta.test", logging.INFO, __file__, 1, message, None, None)


def test_log_records_carry_the_request_and_trace_ids(spans):
    record = _record()
    with request_id_scope("req-abc"), get_tracer().start_as_current_span("work") as span:
        CorrelationFilter().filter(record)
        context = span.get_span_context()

    assert record.request_id == "req-abc"
    assert record.trace_id == format(context.trace_id, "032x")
    assert record.span_id == format(context.span_id, "016x")


def test_log_records_outside_a_request_use_a_placeholder():
    record = _record()
    CorrelationFilter().filter(record)

    assert (record.request_id, record.trace_id) == ("-", "-")


def test_the_json_formatter_emits_one_parseable_object():
    record = _record("battery low")
    with request_id_scope("req-json"):
        CorrelationFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "battery low"
    assert payload["request_id"] == "req-json"
    assert payload["level"] == "INFO" and payload["logger"] == "volta.test"


# --- trace propagation over the queue -----------------------------------------------------


def test_a_consumer_span_continues_the_producers_trace(spans):
    with request_id_scope("req-queue"), get_tracer().start_as_current_span("publish") as producer:
        headers = inject_trace_headers(headers_with_request_id())
    assert "traceparent" in headers and headers["x-request-id"] == "req-queue"

    # The consumer runs later, in another context, with only the message headers.
    with consumer_span("process", headers) as consumer:
        assert get_request_id() == "req-queue"
        assert consumer.get_span_context().trace_id == producer.get_span_context().trace_id

    finished = {span.name: span for span in spans.get_finished_spans()}
    assert finished["process"].parent.span_id == producer.get_span_context().span_id
    assert finished["process"].kind == trace.SpanKind.CONSUMER


def test_headers_arriving_as_bytes_are_understood(spans):
    with get_tracer().start_as_current_span("publish"):
        headers = inject_trace_headers()
    as_bytes = {key: value.encode() for key, value in headers.items()}

    with consumer_span("process", as_bytes) as span:
        assert span.get_span_context().is_valid


def test_a_consumer_span_without_headers_starts_a_new_trace(spans):
    with consumer_span("process", None) as span:
        assert span.get_span_context().is_valid


def test_a_failing_consumer_records_the_error_and_reraises(spans):
    with pytest.raises(RuntimeError, match="boom"):
        with consumer_span("process", {}):
            raise RuntimeError("boom")

    (span,) = [s for s in spans.get_finished_spans() if s.name == "process"]
    assert span.status.status_code == trace.StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_tracing_stays_off_unless_enabled_and_pointed_at_a_collector():
    assert configure_telemetry(Settings(OTEL_ENABLED=False), service_name="t") is False
    assert configure_telemetry(Settings(OTEL_ENABLED=True, OTEL_EXPORTER_OTLP_ENDPOINT=""), service_name="t") is False


# --- worker metrics -----------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, body: bytes, headers: dict | None = None) -> None:
        self.body = body
        self.headers = headers or {}
        self.outcome: str | None = None

    async def ack(self) -> None:
        self.outcome = "ack"

    async def reject(self, requeue: bool = False) -> None:
        self.outcome = "reject"

    async def nack(self, requeue: bool = True) -> None:
        self.outcome = "nack"


class _Store:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rows: list = []

    async def upsert(self, record) -> None:
        if self.fail:
            raise ConnectionError("db down")
        self.rows.append(record)


def _count(outcome: str) -> float:
    return REGISTRY.get_sample_value("volta_ingest_messages_total", {"outcome": outcome}) or 0.0


async def _handle(message: _FakeMessage, store: _Store) -> None:
    await handle_message(message, tolerance_kw=0.05, timescale=store, redis_live=None)


async def test_a_stored_tick_is_acked_and_counted(spans):
    before, store = _count("stored"), _Store()
    message = _FakeMessage(json.dumps(_tick()).encode())

    await _handle(message, store)

    assert message.outcome == "ack" and len(store.rows) == 1
    assert _count("stored") == before + 1
    assert any(span.name == "ingest.process" for span in spans.get_finished_spans())


async def test_a_poison_message_is_rejected_and_counted():
    before = _count("rejected")
    message = _FakeMessage(b"not json")

    await _handle(message, _Store())

    assert message.outcome == "reject"
    assert _count("rejected") == before + 1


async def test_a_storage_failure_requeues_and_is_counted():
    before = _count("requeued")
    message = _FakeMessage(json.dumps(_tick()).encode())

    await _handle(message, _Store(fail=True))

    assert message.outcome == "nack"
    assert _count("requeued") == before + 1


async def test_the_ingest_worker_continues_the_trace_in_the_message_headers(spans):
    with get_tracer().start_as_current_span("simulator.publish") as producer:
        headers = inject_trace_headers()

    await _handle(_FakeMessage(json.dumps(_tick()).encode(), headers), _Store())

    (consumed,) = [s for s in spans.get_finished_spans() if s.name == "ingest.process"]
    assert consumed.context.trace_id == producer.get_span_context().trace_id


# --- /metrics exposition -----------------------------------------------------------------


async def test_the_metrics_endpoint_serves_prometheus_text():
    app = FastAPI()
    app.add_api_route("/metrics", metrics_endpoint)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/metrics")  # answered directly, no redirect to /metrics/

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    for name in (
        "volta_ingest_messages_total",
        "volta_assistant_requests_total",
        "volta_grounding_checks_total",
        "process_cpu_seconds_total",  # CPU per process comes free from the default collector
    ):
        assert name in body


async def test_an_assistant_answer_is_counted(client):
    from app.modules.assistant.deps import get_structured_llm
    from main import app

    def count() -> float:
        return REGISTRY.get_sample_value(
            "volta_assistant_requests_total", {"path": "deterministic", "fallback_reason": "out_of_scope"}
        ) or 0.0

    app.dependency_overrides[get_structured_llm] = lambda: FakeStructuredLLM()
    headers = await auth_headers(client, "obs-metric@example.com")
    await complete_hybrid_onboarding(client, headers)
    before = count()

    response = await client.post("/assistant/me/ask", json={"question": "What's the capital of France?"}, headers=headers)

    assert response.status_code == 200
    assert count() == before + 1


def _ws_open(channel: str) -> float:
    return REGISTRY.get_sample_value("volta_ws_connections", {"channel": channel}) or 0.0


def test_the_websocket_gauge_counts_sessions_and_always_comes_back_down():
    before = _ws_open("energy")

    with track_ws_connection("energy"):
        with track_ws_connection("energy"):
            assert _ws_open("energy") == before + 2
        assert _ws_open("energy") == before + 1

    assert _ws_open("energy") == before


def test_the_websocket_gauge_recovers_when_a_session_fails():
    before = _ws_open("notifications")

    with pytest.raises(RuntimeError):
        with track_ws_connection("notifications"):
            raise RuntimeError("socket died")

    assert _ws_open("notifications") == before
