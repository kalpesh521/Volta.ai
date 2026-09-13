"""CORS / WebSocket origin checks.

The dashboard is served on :8765, but Cursor/WSL port-forwarding often exposes
it as :8766 (or another localhost port). In non-production we accept any
localhost origin so the live WebSocket is not 403'd.
"""
from __future__ import annotations

import re

from app.core.config import settings

LOCALHOST_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    re.IGNORECASE,
)


def is_allowed_origin(origin: str | None) -> bool:
    if not origin:
        return True
    if origin in settings.CORS_ORIGINS:
        return True
    if settings.ENVIRONMENT.lower() != "production":
        return LOCALHOST_ORIGIN_RE.fullmatch(origin) is not None
    return False


def cors_origin_regex() -> str | None:
    if settings.ENVIRONMENT.lower() == "production":
        return None
    # Starlette compiles this without our IGNORECASE flag.
    return r"(?i)^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
