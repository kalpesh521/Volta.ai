"""
Prometheus metrics.

Metric objects are created at import time on the default registry, which already
exports process CPU and memory. Label sets are small and fixed: never use a
household id, user id or free text as a label value.

The API serves `/metrics` when `METRICS_ENABLED`. Workers call
`start_worker_metrics_server`.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest, start_http_server

logger = logging.getLogger("volta.metrics")

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20)

# --- Telemetry ingest ---
INGEST_MESSAGES = Counter(
    "volta_ingest_messages_total",
    "Telemetry messages handled by the ingest worker.",
    ["outcome"],  # stored | rejected | requeued
)
INGEST_SECONDS = Histogram(
    "volta_ingest_process_seconds",
    "Time to validate and store one telemetry message.",
    buckets=_LATENCY_BUCKETS,
)
INGEST_MESSAGE_AGE_SECONDS = Histogram(
    "volta_ingest_message_age_seconds",
    "Age of a tick (now minus its timestamp) when the worker stores it: the effective lag.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)

# --- Realtime ---
WS_CONNECTIONS = Gauge(
    "volta_ws_connections",
    "Open WebSocket connections.",
    ["channel"],  # energy | notifications
)

# --- Assistant ---
ASSISTANT_REQUESTS = Counter(
    "volta_assistant_requests_total",
    "Assistant questions answered.",
    ["path", "fallback_reason"],  # path: llm | deterministic
)
ASSISTANT_SECONDS = Histogram(
    "volta_assistant_latency_seconds",
    "End-to-end assistant latency.",
    ["path"],
    buckets=_LATENCY_BUCKETS,
)
LLM_TOKENS = Counter(
    "volta_llm_tokens_total",
    "LLM tokens used.",
    ["model", "direction"],  # direction: input | output
)
GROUNDING_CHECKS = Counter(
    "volta_grounding_checks_total",
    "Numeric grounding checks on LLM answers.",
    ["result"],  # grounded | ungrounded | skipped
)
ACTION_PROPOSALS = Counter(
    "volta_action_proposals_total",
    "Device action proposals by resulting status.",
    ["status"],
)


@contextmanager
def track_ws_connection(channel: str) -> Iterator[None]:
    """Count an established WebSocket session. The gauge always goes back down, however the session ends."""
    WS_CONNECTIONS.labels(channel=channel).inc()
    try:
        yield
    finally:
        WS_CONNECTIONS.labels(channel=channel).dec()


async def metrics_endpoint() -> Response:
    """Prometheus text format. A plain route, so `/metrics` answers directly (no trailing-slash redirect)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def start_worker_metrics_server(port: int) -> bool:
    """Expose `/metrics` from a worker process. A port of 0 leaves it off."""
    if port <= 0:
        return False
    start_http_server(port)
    logger.info("worker metrics listening on :%d", port)
    return True
