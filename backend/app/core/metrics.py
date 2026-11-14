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
import os
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest, start_http_server

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
    multiprocess_mode="livesum",  # several uvicorn workers per pod: report the pod total
)
HTTP_IN_FLIGHT = Gauge(
    "volta_http_requests_in_flight",
    "HTTP requests being handled right now (streamed answers count until the stream ends). "
    "The autoscaling signal for API pods: the work is mostly waiting on I/O, so CPU stays low.",
    multiprocess_mode="livesum",
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
AGENT_RUNS = Counter(
    "volta_agent_runs_total",
    "Specialist agent runs inside the supervisor.",
    ["agent", "status"],  # status: ok | partial | refused | error | timeout | disabled
)
AGENT_RETRIES = Counter(
    "volta_agent_retries_total",
    "Extra attempts at a planning agent after a transient dependency failure.",
    ["agent"],
)
AGENT_SECONDS = Histogram(
    "volta_agent_latency_seconds",
    "Time a specialist agent took to return its result.",
    ["agent"],
    buckets=_LATENCY_BUCKETS,
)
SUPERVISOR_REQUESTS = Counter(
    "volta_supervisor_requests_total",
    "Supervisor questions answered.",
    ["route", "path"],  # route: single | multi | exclusive | refusal; path: llm | deterministic
)
SUPERVISOR_REGENERATIONS = Counter(
    "volta_supervisor_regenerations_total",
    "Answers regenerated once after failing the numeric grounding check.",
    ["outcome"],  # recovered | fell_back
)
# --- Guardrails, budgets and kill switches (Phase 2) ---
GUARD_DECISIONS = Counter(
    "volta_guard_decisions_total",
    "Input guard verdicts. `observed_*` means the verdict was recorded but not enforced.",
    ["stage", "decision"],  # stage: fast | classifier; decision: allow | review | block | observed_block
)
PII_ENTITIES = Counter(
    "volta_pii_entities_total",
    "Personal-data entities found, by class and where.",
    ["pii_class", "where"],  # where: input | output
)
OUTPUT_VIOLATIONS = Counter(
    "volta_output_violations_total",
    "Model answers withheld or changed by an output guard.",
    ["kind"],  # action_claim | unsafe_advice
)
POLICY_DENIALS = Counter(
    "volta_policy_denials_total",
    "Tool calls refused because the agent is not allowed to use that tool.",
    ["agent", "tool"],
)
BUDGET_EVENTS = Counter(
    "volta_budget_events_total",
    "Token budget outcomes per request.",
    ["tier", "outcome"],  # ok | degraded | rejected | store_error
)
BUDGET_TOKENS = Counter(
    "volta_budget_tokens_debited_total",
    "Tokens debited from user budgets.",
    ["tier"],
)
COOLDOWNS = Counter(
    "volta_guard_cooldowns_total",
    "Cool-downs started after repeated blocked inputs.",
)
KILL_SWITCH_HITS = Counter(
    "volta_kill_switch_hits_total",
    "Requests affected by a disabled feature flag.",
    ["flag"],
)
ACTION_PROPOSALS = Counter(
    "volta_action_proposals_total",
    "Device action proposals by resulting status.",
    ["status"],
)
AUTOMATION_FIRINGS = Counter(
    "volta_automation_firings_total",
    "Automation rule firings by what they did.",
    ["action"],  # notify | suggest | paused
)
AUTOMATION_EVALUATIONS = Counter(
    "volta_automation_evaluations_total",
    "Readings evaluated against a home's active automation rules.",
    ["outcome"],  # evaluated | no_rules | error
)
OUTBOX_DISPATCH = Counter(
    "volta_device_outbox_dispatch_total",
    "Device commands the outbox relay handled, by outcome.",
    ["outcome"],  # sent | retry | dead | blocked | expired
)
OUTBOX_PENDING = Gauge(
    "volta_device_outbox_pending",
    "Device commands waiting in the outbox. A rising value means the relay or the broker is behind.",
)
OUTBOX_DELAY = Histogram(
    "volta_device_outbox_delay_seconds",
    "Time from a command being accepted to it reaching the broker.",
    buckets=_LATENCY_BUCKETS,
)


@contextmanager
def track_ws_connection(channel: str) -> Iterator[None]:
    """Count an established WebSocket session. The gauge always goes back down, however the session ends."""
    WS_CONNECTIONS.labels(channel=channel).inc()
    try:
        yield
    finally:
        WS_CONNECTIONS.labels(channel=channel).dec()


def _multiprocess_registry() -> CollectorRegistry | None:
    """With several uvicorn workers (`python -m app.serve`) each process writes its metrics to files in
    PROMETHEUS_MULTIPROC_DIR; a scrape must read all of them, whichever worker answers it."""
    if not (os.environ.get("PROMETHEUS_MULTIPROC_DIR") or os.environ.get("prometheus_multiproc_dir")):
        return None
    from prometheus_client import multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


async def metrics_endpoint() -> Response:
    """Prometheus text format. A plain route, so `/metrics` answers directly (no trailing-slash redirect)."""
    registry = _multiprocess_registry()
    body = generate_latest(registry) if registry is not None else generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)


def start_worker_metrics_server(port: int) -> bool:
    """Expose `/metrics` from a worker process. A port of 0 leaves it off."""
    if port <= 0:
        return False
    start_http_server(port)
    logger.info("worker metrics listening on :%d", port)
    return True
