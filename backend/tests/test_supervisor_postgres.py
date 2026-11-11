"""
Thread memory on the real Postgres checkpointer. Opt-in, like the other integration tests:

    TEST_POSTGRES_URL=postgresql+asyncpg://user:pass@localhost:5434/db pytest tests/test_supervisor_postgres.py

Each test creates its own schema and drops it, so it never touches application tables.
"""
from __future__ import annotations

import os
import uuid

import pytest

from app.agents_platform.evals.stubs import ScriptedLLM, default_stub_agents
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.supervisor import build_supervisor
from app.ai.checkpointer import CheckpointerProvider, libpq_conninfo
from app.ai.config import AISettings

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")


def runtime() -> SupervisorRuntime:
    return SupervisorRuntime(
        agents=default_stub_agents(), llm=ScriptedLLM(), settings=AISettings(_env_file=None)
    )


@pytest.fixture
async def ai_settings():
    schema = f"lg_sup_{uuid.uuid4().hex[:8]}"
    yield AISettings(
        _env_file=None,
        AI_CHECKPOINT_BACKEND="postgres",
        AI_CHECKPOINT_DATABASE_URL=POSTGRES_URL,
        AI_CHECKPOINT_SCHEMA=schema,
    )
    import psycopg

    with psycopg.connect(libpq_conninfo(POSTGRES_URL), autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def test_a_follow_up_still_works_after_the_process_restarts(ai_settings):
    config = {"configurable": {"thread_id": "chat:u:h:c1"}}

    first = CheckpointerProvider()
    await first.start(ai_settings, auto_setup=True)
    try:
        answered = await build_supervisor(first.saver).ainvoke(
            {"question": "Why is the battery discharging?", "request_id": "r1"}, context=runtime(), config=config
        )
        assert answered["intents"] == ["battery"]
    finally:
        await first.stop()  # the API process goes away

    second = CheckpointerProvider()  # another replica, tables already exist
    await second.start(ai_settings)
    try:
        graph = build_supervisor(second.saver)
        follow_up = await graph.ainvoke({"question": "why?", "request_id": "r2"}, context=runtime(), config=config)

        assert follow_up["classification_method"] == "memory"
        assert follow_up["intents"] == ["battery"]
        assert len(follow_up["turns"]) == 2

        other = await graph.ainvoke(
            {"question": "why?", "request_id": "r3"},
            context=runtime(),
            config={"configurable": {"thread_id": "chat:someone:else:c9"}},
        )
        assert other["classification_method"] != "memory", "threads must not share memory"

        await second.delete_threads(["chat:u:h:c1"])
        assert (await graph.aget_state(config)).values == {}
    finally:
        await second.stop()


async def test_a_multi_agent_turn_checkpoints_and_the_next_turn_starts_clean(ai_settings):
    config = {"configurable": {"thread_id": "chat:u:h:c2"}}
    provider = CheckpointerProvider()
    await provider.start(ai_settings, auto_setup=True)
    try:
        graph = build_supervisor(provider.saver)
        compound = await graph.ainvoke(
            {"question": "What does fault code E04 mean and how is my battery?", "request_id": "r1"},
            context=runtime(),
            config=config,
        )
        assert {r["agent"] for r in compound["results"]} == {"energy_insight", "knowledge"}

        single = await graph.ainvoke(
            {"question": "How much solar did I generate today?", "request_id": "r2"}, context=runtime(), config=config
        )
        assert [r["agent"] for r in single["results"]] == ["energy_insight"], "no results leaked from the last turn"
    finally:
        await provider.stop()
