"""Opt-in real-Redis helpers for the platform tests. Database 15 is wiped, so use a throwaway one:

    TEST_REDIS_URL=redis://localhost:6379/15 pytest
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

from app.agents_platform.infra.redis import RedisProvider

REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not set")


@asynccontextmanager
async def fresh_provider():
    """A provider on its own event loop and a clean database. Created inside the test, never in a fixture,
    because an async client must be closed on the loop that opened it."""
    provider = RedisProvider(REDIS_URL, cooldown_seconds=0.0)
    client = await provider.client()
    assert client is not None, "TEST_REDIS_URL is set but Redis is not reachable"
    await client.flushdb()
    try:
        yield provider
    finally:
        await provider.close()
