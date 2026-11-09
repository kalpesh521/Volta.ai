"""
Process logging: one format, with request and trace ids on every line.

Without this, application loggers (`volta.*`) have no handler and their INFO
lines are dropped. Call `configure_logging` once at process start (API lifespan,
worker `main`).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from opentelemetry import trace

from app.core.request_context import get_request_id

_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [req=%(request_id)s trace=%(trace_id)s] %(message)s"
_CONFIGURED_ATTR = "_volta_configured"


class CorrelationFilter(logging.Filter):
    """Adds `request_id`, `trace_id` and `span_id` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        else:
            record.trace_id = "-"
            record.span_id = "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Idempotent. Safe to call from both the API and worker entry points."""
    root = logging.getLogger()
    if getattr(root, _CONFIGURED_ATTR, False):
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter(_TEXT_FORMAT))

    root.handlers = [handler]
    root.setLevel(level.upper())
    setattr(root, _CONFIGURED_ATTR, True)

    # uvicorn keeps its own handlers; route access/error lines through ours so ids appear there too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
