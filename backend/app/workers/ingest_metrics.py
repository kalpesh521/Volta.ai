"""
Prometheus metrics specific to the batched ingest worker (Phase 5.1).

`volta_ingest_messages_total`, `volta_ingest_process_seconds` and `volta_ingest_message_age_seconds`
stay in `app.core.metrics`. These add what batching and horizontal scaling need. Label sets are fixed.

Queue depth is read from the broker, so every replica reports the same number: aggregate with
`max()`, never `sum()`. For autoscaling, KEDA's RabbitMQ scaler can read the queue length from the
broker directly (see deploy/k8s); these gauges are for dashboards, alerts and a Prometheus-based HPA.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

INGEST_BATCH_SIZE = Histogram(
    "volta_ingest_batch_size",
    "Distinct ticks written per flush.",
    buckets=(1, 2, 5, 10, 25, 50, 100, 250, 500),
)
INGEST_FLUSH_SECONDS = Histogram(
    "volta_ingest_flush_seconds",
    "Time to commit one batch to TimescaleDB (the write only, not the wait for the batch to fill).",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
INGEST_FLUSHES = Counter(
    "volta_ingest_flushes_total",
    "Batch flushes by result.",
    # committed | transient_failure (whole batch requeued) | isolated (batch failed, ticks retried one by one)
    ["outcome"],
)
INGEST_BUFFERED = Gauge(
    "volta_ingest_buffered_messages",
    "Valid ticks held in this replica, received but not yet committed (so not yet acked).",
)
INGEST_QUEUE_DEPTH = Gauge(
    "volta_ingest_queue_depth",
    "Messages ready in the ingest queue, not yet delivered to any consumer (queue lag).",
)
INGEST_QUEUE_CONSUMERS = Gauge(
    "volta_ingest_queue_consumers",
    "Consumers attached to the ingest queue, as reported by the broker.",
)
INGEST_LAG_POLL_ERRORS = Counter(
    "volta_ingest_lag_poll_errors_total",
    "Failed attempts to read the queue depth from the broker.",
)
