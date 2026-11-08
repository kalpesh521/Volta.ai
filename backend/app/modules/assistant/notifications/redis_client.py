"""Shared Redis client for notification cache and pub/sub. Fails open when Redis is down."""
from __future__ import annotations

import asyncio
import logging
import time

from redis.asyncio import Redis, from_url

from app.core.config import settings

logger = logging.getLogger("volta.notifications")

_client: Redis | None = None
_down_until = 0.0


def notification_channel(household_id: str) -> str:
    return f"{settings.NOTIFICATION_KEY_PREFIX}ch:{household_id}"


def notification_list_key(user_id: str, household_id: str) -> str:
    return f"{settings.NOTIFICATION_KEY_PREFIX}list:{user_id}:{household_id}"


async def notification_redis() -> Redis | None:
    """Return a live client, or None for a short cooldown after a connection failure."""
    global _client, _down_until
    if time.monotonic() < _down_until:
        return None
    if _client is not None:
        return _client
    try:
        client = from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.8,
        )
        await client.ping()
    except Exception:
        logger.info("notification redis unavailable; in-app delivery stays in this process")
        _down_until = time.monotonic() + 30
        return None
    _client = client
    return client


def mark_notification_redis_down() -> None:
    global _client, _down_until
    client = _client
    _client = None
    _down_until = time.monotonic() + 30
    if client is None:
        return
    try:
        asyncio.get_running_loop().create_task(client.aclose())
    except RuntimeError:
        return
