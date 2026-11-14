"""
Shared state across uvicorn worker processes (Phase 5.2).

Real separate OS processes, each building the same platform the API builds (`build_platform`), exactly as
`python -m app.serve` workers do. With AI_PLATFORM_STORE=redis, budgets, cool-downs, flag overrides and
conversation locks must behave as one; with `memory` they provably split, which is why the startup guard
exists. The Redis half is opt-in:

    TEST_REDIS_URL=redis://localhost:6379/12 pytest tests/test_shared_state_across_processes.py

It uses unique keys per run and removes them; it never flushes the database. (Flag overrides are one
shared hash, so use a database nothing else is using.)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from app.agents_platform.budgets.tiers import Tier
from app.agents_platform.infra.redis import RedisProvider
from app.agents_platform.platform import build_platform
from app.agents_platform.registry.flags import LLM_FLAG
from app.ai.config import AISettings

BACKEND = Path(__file__).resolve().parents[1]
REDIS_URL = os.environ.get("TEST_REDIS_URL", "")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="TEST_REDIS_URL is not set")

# One worker process: it builds the platform once, then answers commands read from stdin, one JSON line each.
# The commands are the operations the API performs per request. Importing the app takes seconds, so the
# processes are reused for every scenario.
_WORKER = textwrap.dedent(
    """
    import asyncio, json, sys
    from app.agents_platform.budgets.tiers import Tier
    from app.agents_platform.platform import build_platform
    from app.ai.config import get_ai_settings

    async def handle(platform, role, ident):
        if role == "debit":
            for _ in range(25):
                await platform.budget.debit(ident, Tier.FREE, 100)
            return {"used": (await platform.budget.status(ident, Tier.FREE)).used}
        if role == "lock":
            return {"token": await platform.locks.acquire(ident, 60.0)}
        if role == "block":
            return {"cooldown": await platform.abuse.record_block(ident)}
        if role == "cooldown_seen":
            return {"remaining": await platform.abuse.remaining(ident)}
        if role == "flag_off":
            return {"set": await platform.flags.set("ai.llm.enabled", False)}
        raise SystemExit("unknown role " + role)

    async def main():
        platform = build_platform(get_ai_settings())
        print("ready", flush=True)
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line.strip() or line.strip() == "quit":
                return
            role, ident = line.split()
            print(json.dumps(await handle(platform, role, ident)), flush=True)

    asyncio.run(main())
    """
)


class WorkerPool:
    """N real OS processes, like the workers of one pod. `run` sends one command to chosen workers at once."""

    def __init__(self, count: int, *, store: str) -> None:
        env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "AI_PLATFORM_STORE": store,
            "AI_REDIS_URL": REDIS_URL or "redis://127.0.0.1:1/0",  # never reached with the memory store
        }
        self.children = [
            subprocess.Popen(
                [sys.executable, "-c", _WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
                cwd=BACKEND,
            )
            for _ in range(count)
        ]
        for child in self.children:
            assert child.stdout.readline().strip() == "ready"

    def run(self, role: str, ident: str, workers: list[int] | None = None) -> list[dict]:
        chosen = [self.children[i] for i in (range(len(self.children)) if workers is None else workers)]
        for child in chosen:  # release them together
            child.stdin.write(f"{role} {ident}\n")
            child.stdin.flush()
        return [json.loads(child.stdout.readline()) for child in chosen]

    def close(self) -> None:
        for child in self.children:
            try:
                child.stdin.write("quit\n")
                child.stdin.flush()
                child.wait(timeout=20)
            except Exception:
                child.kill()


def _parent_platform(url: str):
    settings = AISettings(AI_PLATFORM_STORE="redis", AI_REDIS_URL=url)
    provider = RedisProvider(url, cooldown_seconds=0.0)
    return build_platform(settings, provider), provider


async def _forget(provider: RedisProvider, ident: str) -> None:
    client = await provider.client()
    if client is None:
        return
    async for key in client.scan_iter(match=f"*{ident}*"):
        await client.delete(key)
    await client.hdel("volta:ai:flags", LLM_FLAG)


# --- what goes wrong without a shared store -------------------------------------------------------


def test_with_the_memory_store_workers_each_see_only_their_own_state():
    pool = WorkerPool(3, store="memory")
    try:
        ident = f"u-{uuid.uuid4().hex[:8]}"
        # 3 workers x 25 x 100 tokens were spent in total, but every worker thinks only 2,500 were.
        assert [r["used"] for r in pool.run("debit", ident)] == [2500, 2500, 2500]

        # Strikes are not pooled either: 3 workers each record one, and none reaches the cool-down.
        assert [r["cooldown"] for r in pool.run("block", ident)] == [0, 0, 0]
        assert [r["remaining"] for r in pool.run("cooldown_seen", ident)] == [0, 0, 0]

        # Every worker takes the same conversation lock: no mutual exclusion.
        assert all(r["token"] for r in pool.run("lock", f"thread-{ident}"))
    finally:
        pool.close()


# --- the shared store holds across processes -----------------------------------------------------


@needs_redis
async def test_budgets_locks_cooldowns_and_flags_behave_as_one_across_four_worker_processes():
    ident = f"u-{uuid.uuid4().hex[:8]}"
    platform, provider = _parent_platform(REDIS_URL)
    pool = await asyncio.to_thread(WorkerPool, 4, store="redis")
    run = lambda *args, **kw: asyncio.to_thread(pool.run, *args, **kw)  # noqa: E731
    try:
        # Budgets: 4 x 25 x 100 tokens from four processes add up with no lost update.
        results = await run("debit", ident)
        assert (await platform.budget.status(ident, Tier.FREE)).used == 10_000
        assert max(r["used"] for r in results) == 10_000  # the last writer saw the pod-wide total

        # Locks: of four simultaneous attempts exactly one wins, and this process is excluded too.
        thread = f"thread-{ident}"
        winners = [r["token"] for r in await run("lock", thread) if r["token"]]
        assert len(winners) == 1
        assert await platform.locks.acquire(thread, 5.0) is None
        await platform.locks.release(thread, winners[0])
        assert await platform.locks.acquire(thread, 5.0)  # and it frees up for everyone

        # Cool-downs: strikes from different workers add up to one cool-down that every process sees.
        limit = platform.settings.AI_GUARD_STRIKE_LIMIT
        strikes = await run("block", ident, workers=list(range(limit)))
        assert sorted(r["cooldown"] > 0 for r in strikes) == [False] * (limit - 1) + [True]
        assert await platform.abuse.remaining(ident) > 0  # this process took no part
        assert all(r["remaining"] > 0 for r in await run("cooldown_seen", ident))  # nor did the fourth worker

        # Kill switch: flipped in one worker, seen here at once.
        assert (await platform.flags.snapshot()).llm_enabled is True
        await run("flag_off", ident, workers=[0])
        assert (await platform.flags.snapshot()).llm_enabled is False
    finally:
        await asyncio.to_thread(pool.close)
        await _forget(provider, ident)
        await provider.close()
