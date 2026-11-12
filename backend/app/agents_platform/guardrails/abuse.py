"""
Progressive cool-down for repeated blocked inputs (doc 04 §6).

A user whose inputs keep being refused as injection attempts is slowed down instead of being allowed to probe
the guard endlessly. Each blocked input is a strike inside a rolling window. From `strike_limit` strikes on,
the user is refused for a cool-down that doubles with every further strike, up to a ceiling:

    limit 3, base 60 s:  3rd strike 60 s · 4th 120 s · 5th 240 s · … · capped at `max_cooldown`

Only injection blocks count. Refusing pasted card numbers is a courtesy to a user's mistake, not abuse.

Fails open: if Redis is unreachable nobody is put in cool-down, because a cache outage must not lock out
users. The in-memory store is per process and meant for development and tests.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from app.agents_platform.infra.redis import KEY_PREFIX, RedisProvider
from app.core.metrics import COOLDOWNS

logger = logging.getLogger("volta.security")


class AbuseStore(Protocol):
    async def add_strike(self, user_key: str, window_seconds: int) -> int: ...

    async def start_cooldown(self, user_key: str, seconds: int) -> None: ...

    async def cooldown_remaining(self, user_key: str) -> int: ...


class InMemoryAbuseStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._strikes: dict[str, tuple[float, int]] = {}  # key -> (window_ends, count)
        self._cooldowns: dict[str, float] = {}

    async def add_strike(self, user_key: str, window_seconds: int) -> int:
        now = self._clock()
        ends, count = self._strikes.get(user_key, (0.0, 0))
        if ends <= now:
            ends, count = now + window_seconds, 0
        self._strikes[user_key] = (ends, count + 1)
        return count + 1

    async def start_cooldown(self, user_key: str, seconds: int) -> None:
        self._cooldowns[user_key] = self._clock() + seconds

    async def cooldown_remaining(self, user_key: str) -> int:
        remaining = self._cooldowns.get(user_key, 0.0) - self._clock()
        if remaining <= 0:
            self._cooldowns.pop(user_key, None)
            return 0
        return int(remaining) + 1


class RedisAbuseStore:
    def __init__(self, provider: RedisProvider) -> None:
        self._provider = provider

    @staticmethod
    def _strike_key(user_key: str) -> str:
        return f"{KEY_PREFIX}abuse:strikes:{user_key}"

    @staticmethod
    def _cooldown_key(user_key: str) -> str:
        return f"{KEY_PREFIX}abuse:cooldown:{user_key}"

    async def add_strike(self, user_key: str, window_seconds: int) -> int:
        client = await self._provider.client()
        if client is None:
            return 0
        key = self._strike_key(user_key)
        try:
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, window_seconds)
            return count
        except Exception:
            logger.warning("abuse store unavailable; strike not recorded", exc_info=True)
            self._provider.mark_down()
            return 0

    async def start_cooldown(self, user_key: str, seconds: int) -> None:
        client = await self._provider.client()
        if client is None:
            return
        try:
            await client.set(self._cooldown_key(user_key), "1", ex=seconds)
        except Exception:
            self._provider.mark_down()

    async def cooldown_remaining(self, user_key: str) -> int:
        client = await self._provider.client()
        if client is None:
            return 0
        try:
            ttl = int(await client.ttl(self._cooldown_key(user_key)))
        except Exception:
            self._provider.mark_down()
            return 0
        return max(ttl, 0)  # -2 (no key) and -1 (no expiry) both mean "not cooling down"


class AbuseGuard:
    def __init__(
        self,
        store: AbuseStore,
        *,
        strike_limit: int,
        window_seconds: int,
        base_cooldown_seconds: int,
        max_cooldown_seconds: int,
    ) -> None:
        self._store = store
        self._limit = max(1, strike_limit)
        self._window = window_seconds
        self._base = base_cooldown_seconds
        self._max = max_cooldown_seconds

    def cooldown_for(self, strikes: int) -> int:
        """Cool-down length after `strikes` strikes in the window; 0 below the limit."""
        if strikes < self._limit:
            return 0
        return min(self._max, self._base * 2 ** (strikes - self._limit))

    async def remaining(self, user_id: object) -> int:
        """Seconds the user must still wait; 0 when free to ask."""
        return await self._store.cooldown_remaining(str(user_id))

    async def record_block(self, user_id: object) -> int:
        """Count one blocked input. Returns the cool-down it started, in seconds (0 if none)."""
        key = str(user_id)
        seconds = self.cooldown_for(await self._store.add_strike(key, self._window))
        if seconds:
            await self._store.start_cooldown(key, seconds)
            COOLDOWNS.inc()
            logger.warning("user placed in cool-down for %ds after repeated blocked inputs", seconds)
        return seconds
