"""
Server-sent events for the streaming endpoint.

Three events, all JSON:

    status   progress while the question is worked on ("Checking your battery…")
    final    the finished answer. This is the only authoritative text: it is sent after the output gate
    error    the request failed; the stream ends

Answer text is **not** streamed token by token. The answer is a structured, five-field object, and the
output gate may replace it (an ungrounded number, a regeneration). Showing words that are later pulled
back is worse than waiting, so the client gets progress first and the checked answer once.
"""
from __future__ import annotations

import json
from typing import Any


def status_event(stage: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"event": "status", "stage": stage, "message": message, **extra}


def encode_sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"
