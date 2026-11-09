"""
OpenTelemetry tracing.

Traces are exported over OTLP/HTTP when `OTEL_ENABLED` is true. When it is off
the helpers below still work (they produce no-op spans), so call sites never
branch on configuration.

Trace context crosses the queue in the standard W3C `traceparent` message
header: producers call `inject_trace_headers`, consumers wrap their work in
`consumer_span`.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.core.config import Settings
from app.core.request_context import REQUEST_ID_HEADER, request_id_scope

logger = logging.getLogger("volta.telemetry")

_TRACER_NAME = "volta"
_provider: Any | None = None


def configure_telemetry(settings: Settings, *, service_name: str, app: Any | None = None) -> bool:
    """Install the tracer provider and library instrumentation. Returns True when tracing is active."""
    global _provider
    if not settings.OTEL_ENABLED:
        return False
    if _provider is not None:
        return True

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT.strip().rstrip("/")
    if not endpoint:
        logger.warning("OTEL_ENABLED is true but OTEL_EXPORTER_OTLP_ENDPOINT is empty; tracing stays off")
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": settings.ENVIRONMENT}
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.OTEL_TRACES_SAMPLER_RATIO)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    _provider = provider

    _instrument_libraries(app)
    logger.info("tracing enabled service=%s endpoint=%s", service_name, endpoint)
    return True


def _instrument_libraries(app: Any | None) -> None:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from app.core.database import engine

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,metrics")


def shutdown_telemetry() -> None:
    """Flush pending spans. Called on process shutdown."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


def inject_trace_headers(headers: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of `headers` with the current `traceparent` added, for a queue message."""
    carrier: dict[str, Any] = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def _extract_context(headers: Mapping[str, Any] | None) -> otel_context.Context:
    carrier = {str(key): _as_text(value) for key, value in (headers or {}).items()}
    return propagate.extract(carrier)


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@contextmanager
def consumer_span(name: str, headers: Mapping[str, Any] | None = None, **attributes: Any) -> Iterator[Span]:
    """
    Span for processing one queue message, parented to the producer's trace.

    Also binds the request id carried in the message headers, so log lines
    written while handling it match the request that produced the message.
    """
    parent = _extract_context(headers)
    request_id = _as_text((headers or {}).get(REQUEST_ID_HEADER.lower())) if headers else None
    with request_id_scope(request_id or None):
        with get_tracer().start_as_current_span(
            name, context=parent, kind=SpanKind.CONSUMER, attributes=attributes or None
        ) as span:
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
