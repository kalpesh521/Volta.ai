"""
One lazily created Redis client for the platform stores (PII vault, token budgets, abuse counters, flags).

Every store that uses it fails *open*: when Redis is unreachable the provider hands out `None` for a short
cooldown and the store falls back to its safe default (no cool-down, no budget check, default flags). A cache
outage must never take the assistant down. Each store documents what "safe" means for it.

The provider is an object, not a module global, so tests point it at their own Redis database and every
client is created on the event loop that uses it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from functools import lru_cache

from redis.asyncio import Redis, from_url

from app.ai.config import get_ai_settings
from app.core.config import settings

logger = logging.getLogger("volta.agents.redis")

KEY_PREFIX = "volta:ai:"


class RedisProvider:
    def __init__(
        self,
        url: str,
        *,
        cooldown_seconds: float = 30.0,
        connect_timeout: float = 0.3,
        operation_timeout: float = 0.8,
    ) -> None:
        self._url = url
        self._cooldown = cooldown_seconds
        self._connect_timeout = connect_timeout
        self._operation_timeout = operation_timeout
        self._client: Redis | None = None
        self._down_until = 0.0

    async def client(self) -> Redis | None:
        """A live client, or None while the cooldown after a failure runs."""
        if time.monotonic() < self._down_until:
            return None
        if self._client is not None:
            return self._client
        try:
            client = from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=self._connect_timeout,
                socket_timeout=self._operation_timeout,
            )
            await client.ping()
        except Exception:
            logger.warning("redis unavailable; platform stores use their fallbacks for %.0fs", self._cooldown)
            self._down_until = time.monotonic() + self._cooldown
            return None
        self._client = client
        return client

    def mark_down(self) -> None:
        """Called by a store after a failed command, so the next calls skip Redis for the cooldown."""
        self._down_until = time.monotonic() + self._cooldown
        client, self._client = self._client, None
        if client is not None:
            try:
                asyncio.get_running_loop().create_task(client.aclose())
            except RuntimeError:  # no running loop: nothing is using the client anyway
                pass

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


@lru_cache
def get_redis_provider() -> RedisProvider:
    return RedisProvider(get_ai_settings().AI_REDIS_URL.strip() or settings.REDIS_URL)
