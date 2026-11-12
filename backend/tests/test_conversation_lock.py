"""
One question at a time per conversation.

The lock stores run against memory always and a real Redis when TEST_REDIS_URL is set. The HTTP tests hold the lock
by hand instead of racing two requests: deterministic, and it avoids two requests sharing one test database session.
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app.agents_platform.infra.redis import RedisProvider
from app.agents_platform.locks import (
    UNLOCKED,
    InMemoryConversationLock,
    RedisConversationLock,
)
from app.agents_platform.service import LOCK_MARGIN_SECONDS, SupervisorService
from main import app
from tests.assistant_helpers import FakeStructuredLLM
from tests.redis_helpers import fresh_provider, needs_redis
from tests.test_assistant_api import _home
from tests.test_supervisor_api import BATTERY_Q, STREAM, V2, ask_v2
from tests.test_supervisor_guardrails_api import post, use_platform
from app.modules.assistant.deps import get_structured_llm
from app.agents_platform.conversations import AssistantConversation, thread_id_for
from sqlalchemy import select


async def check_lock_behaviour(lock) -> None:
    first = await lock.acquire("thread-1", 30)
    assert first and first != UNLOCKED
    assert await lock.acquire("thread-1", 30) is None  # busy
    assert await lock.acquire("thread-2", 30)  # another conversation is independent

    await lock.release("thread-1", "someone-elses-token")  # a stranger cannot free it
    assert await lock.acquire("thread-1", 30) is None

    await lock.release("thread-1", first)
    assert await lock.acquire("thread-1", 30)  # free again


async def test_the_memory_lock():
    await check_lock_behaviour(InMemoryConversationLock())


@needs_redis
async def test_the_redis_lock_behaves_identically():
    async with fresh_provider() as provider:
        await check_lock_behaviour(RedisConversationLock(provider))


async def test_a_memory_lease_expires_so_a_crashed_request_cannot_lock_a_conversation_forever():
    now = [100.0]
    lock = InMemoryConversationLock(clock=lambda: now[0])
    await lock.acquire("t", 35)
    now[0] = 134.0
    assert await lock.acquire("t", 35) is None
    now[0] = 136.0
    assert await lock.acquire("t", 35)


@needs_redis
async def test_a_redis_lease_expires_by_itself():
    async with fresh_provider() as provider:
        lock = RedisConversationLock(provider)
        await lock.acquire("t", 0.2)
        assert await lock.acquire("t", 0.2) is None
        await asyncio.sleep(0.6)
        assert await lock.acquire("t", 30)


@needs_redis
async def test_a_slow_holder_cannot_release_a_lock_a_newer_request_took_after_its_lease_ran_out():
    async with fresh_provider() as provider:
        lock = RedisConversationLock(provider)
        slow = await lock.acquire("t", 0.2)
        await asyncio.sleep(0.6)  # the first lease ran out
        newer = await lock.acquire("t", 30)
        assert newer

        await lock.release("t", slow)  # the slow request finally finishes
        assert await lock.acquire("t", 30) is None  # the newer request still holds it


async def test_redis_down_means_no_lock_and_never_a_refused_question():
    lock = RedisConversationLock(RedisProvider("redis://127.0.0.1:1/0", cooldown_seconds=30.0))
    first = await lock.acquire("t", 30)
    second = await lock.acquire("t", 30)
    assert first == UNLOCKED and second == UNLOCKED
    await lock.release("t", first)  # must not raise


def test_the_lease_is_longer_than_the_request_deadline():
    assert LOCK_MARGIN_SECONDS > 0


# --- over HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def fake_llm(client: AsyncClient) -> FakeStructuredLLM:
    fake = FakeStructuredLLM()
    app.dependency_overrides[get_structured_llm] = lambda: fake
    return fake


async def thread_of(db_session, conversation_id: str) -> str:
    row = (await db_session.execute(select(AssistantConversation).where(AssistantConversation.id == conversation_id))).scalar_one()
    return thread_id_for(row)


async def test_a_second_question_on_a_busy_conversation_is_refused_and_works_once_it_is_free(
    client, db_session, db_scope, fake_llm
):
    platform = use_platform()
    headers, _, _ = await _home(client, "lock-busy@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)
    thread = await thread_of(db_session, first["conversation_id"])

    held = await platform.locks.acquire(thread, 30)  # a question is "still being answered"
    busy = await post(client, headers, "why?", conversation_id=first["conversation_id"])
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "conversation_busy"
    stream = await post(client, headers, "why?", path=STREAM, conversation_id=first["conversation_id"])
    assert stream.status_code == 409  # refused before the first byte of the stream

    await platform.locks.release(thread, held)
    assert (await post(client, headers, "why?", conversation_id=first["conversation_id"])).status_code == 200


async def test_the_lock_is_free_again_after_every_successful_answer(client, db_session, fake_llm):
    platform = use_platform()
    headers, _, _ = await _home(client, "lock-release@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)
    thread = await thread_of(db_session, first["conversation_id"])

    await ask_v2(client, headers, "why?", first["conversation_id"])
    probe = await platform.locks.acquire(thread, 30)
    assert probe is not None, "the lock leaked after a normal answer"


async def test_the_lock_is_free_again_after_a_failed_answer(client, db_session, fake_llm, monkeypatch):
    platform = use_platform()
    headers, _, _ = await _home(client, "lock-failure@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)
    thread = await thread_of(db_session, first["conversation_id"])

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(SupervisorService, "_finish", explode)
    with pytest.raises(RuntimeError):
        await client.post(V2, json={"question": "why?", "conversation_id": first["conversation_id"]}, headers=headers)

    probe = await platform.locks.acquire(thread, 30)
    assert probe is not None, "the lock leaked after a failure"


async def test_a_new_conversation_is_never_blocked_and_different_conversations_do_not_interfere(client, db_session, fake_llm):
    platform = use_platform()
    headers, _, _ = await _home(client, "lock-independent@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)
    held = await platform.locks.acquire(await thread_of(db_session, first["conversation_id"]), 30)
    assert held

    fresh = await ask_v2(client, headers, BATTERY_Q)  # no conversation id: a new thread
    assert fresh["conversation_id"] != first["conversation_id"]


async def test_a_refused_question_does_not_free_the_lock_the_first_one_holds(client, db_session, fake_llm):
    platform = use_platform()
    headers, _, _ = await _home(client, "lock-holder@example.com")
    first = await ask_v2(client, headers, BATTERY_Q)
    thread = await thread_of(db_session, first["conversation_id"])
    held = await platform.locks.acquire(thread, 30)

    await post(client, headers, "why?", conversation_id=first["conversation_id"])  # refused with 409

    assert await platform.locks.acquire(thread, 30) is None  # still held by the original request
    await platform.locks.release(thread, held)
