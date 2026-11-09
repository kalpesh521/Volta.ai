"""
Shared slowapi limiter.

Counters live in Redis when `RATE_LIMIT_STORAGE_URI` is set, so the limit holds
across API replicas and restarts. If Redis becomes unreachable the limiter falls
back to per-process counters instead of failing requests.

Authenticated callers are limited per user (a shared office or mobile-carrier IP
must not share one bucket), everyone else per client IP.
"""
from __future__ import annotations

import jwt
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings
from app.core.security import TokenType, decode_token

_KEY_PREFIX = "volta:ratelimit"
_BEARER = "bearer "


def rate_limit_key(request: Request) -> str:
    """`user:<id>` for a valid access token, otherwise `ip:<address>`."""
    header = request.headers.get("authorization", "")
    if header[: len(_BEARER)].lower() == _BEARER:
        try:
            claims = decode_token(header[len(_BEARER):].strip())
        except jwt.PyJWTError:
            claims = None
        if claims and claims.get("type") == TokenType.ACCESS and claims.get("sub"):
            return f"user:{claims['sub']}"
    return f"ip:{get_remote_address(request)}"


def build_limiter(storage_uri: str = "", *, key_prefix: str = _KEY_PREFIX) -> Limiter:
    """A limiter backed by Redis when `storage_uri` is set, otherwise by this process's memory."""
    return Limiter(
        key_func=rate_limit_key,
        storage_uri=storage_uri.strip() or None,
        in_memory_fallback_enabled=True,
        key_prefix=key_prefix,
    )


limiter = build_limiter(settings.RATE_LIMIT_STORAGE_URI)
