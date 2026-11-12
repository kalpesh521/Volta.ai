"""
Where the daily token counters live.

One counter per user per UTC day. The key carries the date, so yesterday's counter is simply never read again
and expires by itself (48 hours, so a request that straddles midnight still finds its counter).

Both stores raise nothing the caller has to handle: a Redis failure reads as "nothing used yet" and a debit is
dropped. That is the fail-open choice (doc 04 §6): a cache outage must not stop users from getting answers. The
cost is bounded by the per-request output limits and the request rate limit, and `store_error` is counted so
the outage is visible.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from app.agents_platform.infra.redis import KEY_PREFIX, RedisProvider

logger = logging.getLogger("volta.agents.budget")

COUNTER_TTL_SECONDS = 48 * 3600


class BudgetStore(Protocol):
    async def used(self, key: str) -> int | None:
        """Tokens used so far, or None when the store cannot say (treated as fail-open)."""

    async def add(self, key: str, tokens: int) -> bool:
        """Add to the counter. False when it could not be recorded."""


class InMemoryBudgetStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._counters: dict[str, tuple[float, int]] = {}  # key -> (expires_at, tokens)

    async def used(self, key: str) -> int | None:
        expires, tokens = self._counters.get(key, (0.0, 0))
        if expires <= self._clock():
            self._counters.pop(key, None)
            return 0
        return tokens

    async def add(self, key: str, tokens: int) -> bool:
        now = self._clock()
        expires, current = self._counters.get(key, (0.0, 0))
        if expires <= now:
            expires, current = now + COUNTER_TTL_SECONDS, 0
        self._counters[key] = (expires, current + tokens)
        return True


class RedisBudgetStore:
    def __init__(self, provider: RedisProvider) -> None:
        self._provider = provider

    async def used(self, key: str) -> int | None:
        client = await self._provider.client()
        if client is None:
            return None
        try:
            value = await client.get(f"{KEY_PREFIX}budget:{key}")
        except Exception:
            logger.warning("budget store read failed", exc_info=True)
            self._provider.mark_down()
            return None
        return int(value) if value is not None else 0

    async def add(self, key: str, tokens: int) -> bool:
        client = await self._provider.client()
        if client is None:
            return False
        redis_key = f"{KEY_PREFIX}budget:{key}"
        try:
            async with client.pipeline(transaction=True) as pipe:
                pipe.incrby(redis_key, tokens)
                pipe.expire(redis_key, COUNTER_TTL_SECONDS)
                await pipe.execute()
        except Exception:
            logger.warning("budget store write failed", exc_info=True)
            self._provider.mark_down()
            return False
        return True
