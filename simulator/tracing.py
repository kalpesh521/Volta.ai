"""
W3C Trace Context for the simulator, so a trace starts at the source (Phase 5.5).

The backend continues a trace from the `traceparent` message header (`consumer_span` in
backend/app/core/telemetry.py). The simulator has no tracing SDK and exports no spans of its own: it only
mints a trace id and a span id per telemetry message and sends them as the parent. In the tracing backend the
`ingest.process` span of a tick therefore shows up under that trace id with a parent span that was never
exported (it is the "publish" step at the source). That is enough to find every hop of one tick by trace id,
and to see how long the tick sat in the queue from the gap between the tick time and the consumer span.

Format (https://www.w3.org/TR/trace-context/):  00-<32 hex trace id>-<16 hex parent id>-<2 hex flags>
Flag 01 = sampled. The backend honours the parent's flag (ParentBased sampler), so the ratio chosen here
decides whether the backend records the trace.
"""
from __future__ import annotations

import random
import re
import secrets
from collections.abc import Callable

TRACEPARENT_HEADER = "traceparent"
REQUEST_ID_HEADER = "x-request-id"

_VALID = re.compile(r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$")


def _non_zero_hex(n_bytes: int) -> str:
    """Random hex that is not all zeros (the spec forbids an all-zero id)."""
    while True:
        value = secrets.token_hex(n_bytes)
        if int(value, 16) != 0:
            return value


def new_traceparent(sample_ratio: float = 1.0, rng: Callable[[], float] = random.random) -> str:
    """A fresh `traceparent`. `sample_ratio` is the chance the trace is flagged as sampled (clamped to 0..1)."""
    ratio = min(1.0, max(0.0, sample_ratio))
    flags = "01" if rng() < ratio else "00"
    return f"00-{_non_zero_hex(16)}-{_non_zero_hex(8)}-{flags}"


def is_valid_traceparent(value: str) -> bool:
    match = _VALID.match(value or "")
    return bool(match) and int(match["trace"], 16) != 0 and int(match["span"], 16) != 0


def trace_headers(sample_ratio: float = 1.0) -> dict[str, str]:
    """
    Headers for one outgoing message: a new `traceparent`, and an `x-request-id` equal to the trace id so log
    lines written by the backend while handling the tick carry the same identifier.
    """
    traceparent = new_traceparent(sample_ratio)
    return {TRACEPARENT_HEADER: traceparent, REQUEST_ID_HEADER: traceparent.split("-")[1]}
