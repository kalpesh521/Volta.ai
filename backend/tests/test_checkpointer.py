"""Checkpointer provider. The Postgres test needs a real database and is opt-in:

    TEST_POSTGRES_URL=postgresql+asyncpg://volta:volta@localhost:5434/volta_auth pytest tests/test_checkpointer.py
"""
from __future__ import annotations

import os
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from app.ai.checkpointer import CheckpointerError, CheckpointerProvider, libpq_conninfo
from app.ai.config import AISettings

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


def test_conninfo_drops_the_async_driver_and_keeps_credentials():
    conninfo = libpq_conninfo("postgresql+asyncpg://volta:p%40ss@db.internal:5434/volta_auth")
    assert conninfo == "postgresql://volta:p%40ss@db.internal:5434/volta_auth"


def test_conninfo_rejects_non_postgres_urls():
    with pytest.raises(CheckpointerError):
        libpq_conninfo("sqlite+aiosqlite:///:memory:")


async def test_memory_backend_is_the_default_and_always_reachable():
    provider = CheckpointerProvider()
    await provider.start(AISettings(AI_CHECKPOINT_BACKEND="memory"))

    assert provider.backend == "memory"
    assert isinstance(provider.saver, InMemorySaver)
    assert await provider.ping() is True
    await provider.stop()


async def test_saver_is_usable_before_start_for_tests():
    assert isinstance(CheckpointerProvider().saver, InMemorySaver)


@pytest.mark.parametrize("schema", ["Bad-Name", "1abc", "a;drop schema public", ""])
async def test_invalid_schema_names_are_refused(schema):
    provider = CheckpointerProvider()
    with pytest.raises(CheckpointerError):
        await provider.start(AISettings(AI_CHECKPOINT_BACKEND="postgres", AI_CHECKPOINT_SCHEMA=schema))


class _State(TypedDict, total=False):
    confirmed: bool


def _graph(saver):
    def ask(state: _State) -> _State:
        answer = interrupt({"need": "confirmation"})
        return {"confirmed": bool(answer.get("confirmed"))}

    graph = StateGraph(_State)
    graph.add_node("ask", ask)
    graph.add_edge(START, "ask")
    graph.add_edge("ask", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")
async def test_postgres_pending_confirmation_survives_a_restart():
    schema = f"lg_test_{uuid.uuid4().hex[:8]}"
    ai = AISettings(
        AI_CHECKPOINT_BACKEND="postgres",
        AI_CHECKPOINT_DATABASE_URL=POSTGRES_URL,
        AI_CHECKPOINT_SCHEMA=schema,
    )
    config = {"configurable": {"thread_id": "proposal-1"}}

    first = CheckpointerProvider()
    await first.start(ai, auto_setup=True)
    try:
        paused = await _graph(first.saver).ainvoke({}, config)
        assert "__interrupt__" in paused
        await first.stop()  # the process goes away

        second = CheckpointerProvider()  # another replica, tables already exist
        await second.start(ai)
        graph = _graph(second.saver)
        assert (await graph.aget_state(config)).next == ("ask",)

        done = await graph.ainvoke(Command(resume={"confirmed": True}), config)
        assert done["confirmed"] is True
        assert (await graph.aget_state(config)).next == ()  # finished: cannot be resumed again

        await second.delete_threads(["proposal-1"])
        assert (await graph.aget_state(config)).values == {}
        assert await second.ping() is True
        await second.stop()
    finally:
        import psycopg

        from app.ai.checkpointer import libpq_conninfo

        with psycopg.connect(libpq_conninfo(POSTGRES_URL), autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
