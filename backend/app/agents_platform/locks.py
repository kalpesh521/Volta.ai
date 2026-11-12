"""
One question at a time per conversation.

A conversation is one LangGraph thread. Two questions running on it at once (a double-submit, a retry racing the
original) would both start from the same checkpoint and write two branches, and the later one would win: a turn
silently vanishes from the memory that follow-ups depend on. So a second question on a conversation that is still
being answered is refused with `409 conversation_busy` instead of racing.

The lock is a lease, not a promise: it expires after the request's deadline plus a margin, so a crashed process can
never leave a conversation locked for good. Only the holder can release it (a token is compared), so a slow request
cannot free a lock a newer request already took after the lease ran out.

Fails open: if Redis is unreachable the question is allowed through unlocked. The cost is the rare lost turn this
module exists to prevent; the alternative, refusing every question during a cache outage, is worse.
"""
from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from app.agents_platform.infra.redis import KEY_PREFIX, RedisProvider
from app.core.exceptions import AppError

logger = logging.getLogger("volta.agents.locks")

UNLOCKED = "unlocked"  # the token handed out when the store could not lock: releasing it is a no-op


class ConversationBusyError(AppError):
    status_code = 409
    code = "conversation_busy"

    def __init__(self) -> None:
        super().__init__("Your previous message in this conversation is still being answered. Please wait a moment.")


class ConversationLock(Protocol):
    async def acquire(self, thread_id: str, lease_seconds: float) -> str | None:
        """A token when the lock is taken (or cannot be checked), None when someone else holds it."""

    async def release(self, thread_id: str, token: str) -> None: ...


class InMemoryConversationLock:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: dict[str, tuple[str, float]] = {}  # thread -> (token, lease ends)

    async def acquire(self, thread_id: str, lease_seconds: float) -> str | None:
        held = self._held.get(thread_id)
        if held is not None and held[1] > self._clock():
            return None
        token = uuid.uuid4().hex
        self._held[thread_id] = (token, self._clock() + lease_seconds)
        return token

    async def release(self, thread_id: str, token: str) -> None:
        held = self._held.get(thread_id)
        if held is not None and held[0] == token:
            del self._held[thread_id]


# Delete the key only if it still holds our token: a compare-and-delete that must be one atomic step.
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisConversationLock:
    def __init__(self, provider: RedisProvider) -> None:
        self._provider = provider

    @staticmethod
    def _key(thread_id: str) -> str:
        return f"{KEY_PREFIX}lock:{thread_id}"

    async def acquire(self, thread_id: str, lease_seconds: float) -> str | None:
        client = await self._provider.client()
        if client is None:
            return UNLOCKED
        token = uuid.uuid4().hex
        try:
            taken = await client.set(self._key(thread_id), token, nx=True, px=max(1, int(lease_seconds * 1000)))
        except Exception:
            logger.warning("conversation lock unavailable; continuing without it", exc_info=True)
            self._provider.mark_down()
            return UNLOCKED
        return token if taken else None

    async def release(self, thread_id: str, token: str) -> None:
        if token == UNLOCKED:
            return
        client = await self._provider.client()
        if client is None:
            return
        try:
            await client.eval(_RELEASE, 1, self._key(thread_id), token)
        except Exception:
            self._provider.mark_down()  # the lease expires by itself
