"""
Request correlation.

Every request gets one id. It is read from `X-Request-ID` when the caller sent a
well-formed one, otherwise generated, then echoed on the response and attached
to every log line through a context variable. Workers set the same variable from
the message they are processing, so one id can be followed across the API, the
queue and the workers.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"

# Printable, header-safe, bounded. Anything else is replaced, never trusted.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

_request_id: ContextVar[str | None] = ContextVar("volta_request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(value: str | None) -> str:
    """Return `value` when it is a safe id, otherwise a fresh one."""
    candidate = (value or "").strip()
    return candidate if _VALID_REQUEST_ID.match(candidate) else new_request_id()


def get_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def request_id_scope(request_id: str | None = None) -> Iterator[str]:
    """Bind a request id for the duration of a unit of work (a queue message, a job)."""
    resolved = sanitize_request_id(request_id)
    token = _request_id.set(resolved)
    try:
        yield resolved
    finally:
        _request_id.reset(token)


class RequestContextMiddleware:
    """Pure ASGI middleware. Pure ASGI (not BaseHTTPMiddleware) so streaming and WebSockets are untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = sanitize_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)


def headers_with_request_id(headers: MutableMapping[str, object] | None = None) -> dict[str, object]:
    """Copy of `headers` carrying the current request id, for outgoing queue messages."""
    merged: dict[str, object] = dict(headers or {})
    current = get_request_id()
    if current:
        merged[REQUEST_ID_HEADER.lower()] = current
    return merged
