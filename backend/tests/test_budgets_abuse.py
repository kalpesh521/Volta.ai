"""
Token budgets (gate G3) and the cool-down after repeated refused inputs.

The stores run against memory always and against a real Redis when TEST_REDIS_URL is set, with the same
assertions, so the two cannot drift apart.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.agents_platform.budgets.service import TokenBudget
from app.agents_platform.budgets.store import InMemoryBudgetStore, RedisBudgetStore
from app.agents_platform.budgets.tiers import Tier, daily_token_limit
from app.agents_platform.guardrails.abuse import AbuseGuard, InMemoryAbuseStore, RedisAbuseStore
from app.agents_platform.infra.redis import RedisProvider
from app.ai.config import AISettings
from tests.redis_helpers import fresh_provider, needs_redis

SETTINGS = AISettings(_env_file=None, AI_BUDGET_DAILY_TOKENS_FREE=1000, AI_BUDGET_DAILY_TOKENS_PLUS=5000)
USER = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc)


# --- tiers ----------------------------------------------------------------------------------------


def test_an_unknown_or_missing_tier_is_the_restricted_one():
    assert Tier.parse("plus") is Tier.PLUS
    assert Tier.parse(None) is Tier.FREE
    assert Tier.parse("enterprise") is Tier.FREE
    assert Tier.parse("") is Tier.FREE


def test_limits_come_from_settings():
    assert daily_token_limit(Tier.FREE, SETTINGS) == 1000
    assert daily_token_limit(Tier.PLUS, SETTINGS) == 5000


# --- the budget over each store -------------------------------------------------------------------


async def check_budget_behaviour(store) -> None:
    clock = Clock(at(12))
    budget = TokenBudget(store, SETTINGS, clock=clock)

    fresh = await budget.status(USER, Tier.FREE)
    assert (fresh.limit, fresh.used, fresh.remaining, fresh.exhausted, fresh.known) == (1000, 0, 1000, False, True)

    await budget.debit(USER, Tier.FREE, 400)
    await budget.debit(USER, Tier.FREE, 250)
    partway = await budget.status(USER, Tier.FREE)
    assert (partway.used, partway.remaining, partway.exhausted) == (650, 350, False)

    await budget.debit(USER, Tier.FREE, 350)
    assert (await budget.status(USER, Tier.FREE)).exhausted is True  # at the limit counts as spent

    other = uuid.uuid4()
    assert (await budget.status(other, Tier.FREE)).used == 0  # users never share a counter

    clock.now = at(13, hour=0)  # the next UTC day starts from zero
    assert (await budget.status(USER, Tier.FREE)).used == 0


async def test_the_memory_budget():
    await check_budget_behaviour(InMemoryBudgetStore())


@needs_redis
async def test_the_redis_budget_behaves_identically():
    async with fresh_provider() as provider:
        await check_budget_behaviour(RedisBudgetStore(provider))


@needs_redis
async def test_the_redis_counter_expires_by_itself():
    async with fresh_provider() as provider:
        store = RedisBudgetStore(provider)
        await store.add("u:20261112", 10)
        ttl = await (await provider.client()).ttl("volta:ai:budget:u:20261112")
        assert 0 < ttl <= 48 * 3600


async def test_seconds_until_reset_is_the_time_to_the_next_utc_midnight():
    budget = TokenBudget(InMemoryBudgetStore(), SETTINGS, clock=lambda: at(12, hour=22))
    assert (await budget.status(USER, Tier.FREE)).seconds_until_reset == 2 * 3600


async def test_zero_or_negative_debits_are_ignored():
    store = InMemoryBudgetStore()
    budget = TokenBudget(store, SETTINGS, clock=lambda: at(12))
    await budget.debit(USER, Tier.FREE, 0)
    await budget.debit(USER, Tier.FREE, -5)
    assert (await budget.status(USER, Tier.FREE)).used == 0


async def test_a_down_store_is_fail_open_and_visibly_unknown():
    down = RedisProvider("redis://127.0.0.1:1/0", cooldown_seconds=30.0)
    budget = TokenBudget(RedisBudgetStore(down), SETTINGS, clock=lambda: at(12))

    status = await budget.status(USER, Tier.FREE)
    assert status.known is False and status.exhausted is False  # never blocks a user because Redis is down
    await budget.debit(USER, Tier.FREE, 100)  # must not raise


# --- cool-down ------------------------------------------------------------------------------------


def guard(store, **overrides) -> AbuseGuard:
    params = dict(strike_limit=3, window_seconds=600, base_cooldown_seconds=60, max_cooldown_seconds=500)
    return AbuseGuard(store, **{**params, **overrides})


def test_the_cooldown_doubles_with_each_strike_past_the_limit_up_to_the_ceiling():
    g = guard(InMemoryAbuseStore())
    assert [g.cooldown_for(n) for n in range(0, 9)] == [0, 0, 0, 60, 120, 240, 480, 500, 500]


async def check_abuse_behaviour(store) -> None:
    g = guard(store)
    assert await g.remaining(USER) == 0
    assert [await g.record_block(USER) for _ in range(5)] == [0, 0, 60, 120, 240]
    assert 0 < await g.remaining(USER) <= 240  # the longest cool-down is running
    assert await g.remaining(uuid.uuid4()) == 0  # another user is unaffected


async def test_the_memory_cooldown():
    await check_abuse_behaviour(InMemoryAbuseStore())


@needs_redis
async def test_the_redis_cooldown_behaves_identically():
    async with fresh_provider() as provider:
        await check_abuse_behaviour(RedisAbuseStore(provider))


async def test_strikes_expire_with_their_window():
    now = [1000.0]
    g = guard(InMemoryAbuseStore(clock=lambda: now[0]), window_seconds=100)
    await g.record_block(USER)
    await g.record_block(USER)
    now[0] += 101  # the window passed: those two strikes no longer count
    assert await g.record_block(USER) == 0


async def test_the_cooldown_ends():
    now = [1000.0]
    g = guard(InMemoryAbuseStore(clock=lambda: now[0]))
    for _ in range(3):
        await g.record_block(USER)
    assert await g.remaining(USER) > 0
    now[0] += 61
    assert await g.remaining(USER) == 0


async def test_a_down_store_never_locks_anyone_out():
    down = RedisProvider("redis://127.0.0.1:1/0", cooldown_seconds=30.0)
    g = guard(RedisAbuseStore(down))
    assert [await g.record_block(USER) for _ in range(5)] == [0] * 5
    assert await g.remaining(USER) == 0


def test_a_strike_limit_of_zero_is_treated_as_one():
    assert guard(InMemoryAbuseStore(), strike_limit=0).cooldown_for(1) == 60
