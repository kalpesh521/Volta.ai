"""
Where the original values wait between masking a question and restoring the answer.

* `InMemoryVault` keeps them in this process, with a time limit. Mask and restore happen in one request, so
  for a single API process this is correct and needs nothing else.
* `RedisVault` keeps them encrypted (AES-256-GCM) in Redis with a time limit. It exists for the day masking
  and restoring run in different processes (a confirmation notification, an ambient agent), without ever
  putting raw values into a checkpoint. Each value is bound to its request and token as authenticated data,
  so a value copied under another key does not decrypt.

Neither backend is ever written to a LangGraph checkpoint, a log line or a trace.

Redis fails *open*: if it is down, nothing is stored, the answer simply keeps `[hidden …]` where a value
would have been restored. Nothing leaks and the question is still answered.
"""
from __future__ import annotations

import base64
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.agents_platform.infra.redis import KEY_PREFIX, RedisProvider

logger = logging.getLogger("volta.agents.pii")

_NONCE_BYTES = 12


class Vault(Protocol):
    async def store(self, request_id: str, tokens: Mapping[str, str]) -> bool: ...

    async def load(self, request_id: str) -> dict[str, str]: ...

    async def discard(self, request_id: str) -> None: ...


class InMemoryVault:
    def __init__(
        self, *, ttl_seconds: int = 900, max_requests: int = 10_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_requests
        self._clock = clock
        self._items: dict[str, tuple[float, dict[str, str]]] = {}

    def _purge(self) -> None:
        now = self._clock()
        for request_id in [k for k, (expires, _) in self._items.items() if expires <= now]:
            del self._items[request_id]
        while len(self._items) >= self._max:  # a hard ceiling: drop the oldest rather than grow
            del self._items[next(iter(self._items))]

    async def store(self, request_id: str, tokens: Mapping[str, str]) -> bool:
        if not tokens:
            return True
        self._purge()
        self._items[request_id] = (self._clock() + self._ttl, dict(tokens))
        return True

    async def load(self, request_id: str) -> dict[str, str]:
        entry = self._items.get(request_id)
        if entry is None or entry[0] <= self._clock():
            self._items.pop(request_id, None)
            return {}
        return dict(entry[1])

    async def discard(self, request_id: str) -> None:
        self._items.pop(request_id, None)


def parse_key(encoded: str) -> bytes:
    """A urlsafe-base64 string of exactly 32 bytes."""
    key = base64.urlsafe_b64decode(encoded.encode())
    if len(key) != 32:
        raise ValueError("AI_PII_VAULT_KEY must decode to exactly 32 bytes")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


class RedisVault:
    def __init__(self, provider: RedisProvider, *, key: bytes, ttl_seconds: int = 900) -> None:
        self._provider = provider
        self._aead = AESGCM(key)
        self._ttl = ttl_seconds

    @staticmethod
    def _redis_key(request_id: str) -> str:
        return f"{KEY_PREFIX}pii:{request_id}"

    def _seal(self, request_id: str, token: str, value: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, value.encode(), f"{request_id}|{token}".encode())
        return base64.b64encode(nonce + sealed).decode()

    def _open(self, request_id: str, token: str, blob: str) -> str | None:
        raw = base64.b64decode(blob)
        try:
            return self._aead.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], f"{request_id}|{token}".encode()).decode()
        except InvalidTag:
            logger.warning("vault entry failed authentication and was ignored")
            return None

    async def store(self, request_id: str, tokens: Mapping[str, str]) -> bool:
        if not tokens:
            return True
        client = await self._provider.client()
        if client is None:
            return False
        key = self._redis_key(request_id)
        try:
            async with client.pipeline(transaction=True) as pipe:
                pipe.hset(key, mapping={t: self._seal(request_id, t, v) for t, v in tokens.items()})
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except Exception:
            logger.warning("vault store failed; the answer will not restore personal values", exc_info=True)
            self._provider.mark_down()
            return False
        return True

    async def load(self, request_id: str) -> dict[str, str]:
        client = await self._provider.client()
        if client is None:
            return {}
        try:
            blobs = await client.hgetall(self._redis_key(request_id))
        except Exception:
            logger.warning("vault load failed", exc_info=True)
            self._provider.mark_down()
            return {}
        opened = {t: self._open(request_id, t, b) for t, b in blobs.items()}
        return {t: v for t, v in opened.items() if v is not None}

    async def discard(self, request_id: str) -> None:
        client = await self._provider.client()
        if client is None:
            return
        try:
            await client.delete(self._redis_key(request_id))
        except Exception:
            self._provider.mark_down()
