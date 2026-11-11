"""Conversation ownership and retention."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from typing_extensions import TypedDict

from app.agents_platform.conversations import (
    AssistantConversation,
    ConversationNotFoundError,
    ConversationRepository,
    thread_id_for,
)
from app.agents_platform.maintenance import prune_conversations
from app.ai.checkpointer import CheckpointerProvider

HOME = "home_aaaaaaaaaaaa"


async def test_a_new_conversation_gets_a_server_issued_id(db_session):
    repo = ConversationRepository(db_session)
    a = await repo.open(None, user_id=uuid.uuid4(), household_id=HOME)
    b = await repo.open(None, user_id=uuid.uuid4(), household_id=HOME)
    assert a.id != b.id and len(a.id) == 32


async def test_the_owner_can_resume_and_the_thread_id_is_scoped_to_user_and_home(db_session):
    repo = ConversationRepository(db_session)
    user = uuid.uuid4()
    created = await repo.open(None, user_id=user, household_id=HOME)

    resumed = await repo.open(created.id, user_id=user, household_id=HOME)

    assert resumed == created
    assert resumed.thread_id == f"chat:{user}:{HOME}:{created.id}"


@pytest.mark.parametrize("who", ["other_user", "other_home", "unknown_id"])
async def test_anyone_else_gets_the_same_not_found(db_session, who):
    repo = ConversationRepository(db_session)
    owner = uuid.uuid4()
    created = await repo.open(None, user_id=owner, household_id=HOME)
    attempts = {
        "other_user": (created.id, uuid.uuid4(), HOME),
        "other_home": (created.id, owner, "home_bbbbbbbbbbbb"),
        "unknown_id": ("f" * 32, owner, HOME),
    }
    conversation_id, user, home = attempts[who]

    with pytest.raises(ConversationNotFoundError):
        await repo.open(conversation_id, user_id=user, household_id=home)


async def test_touch_moves_last_activity_forward(db_session):
    repo = ConversationRepository(db_session)
    created = await repo.open(None, user_id=uuid.uuid4(), household_id=HOME)
    later = datetime.now(timezone.utc) + timedelta(days=3)

    await repo.touch(created.id, now=later)

    row = (await db_session.execute(select(AssistantConversation).where(AssistantConversation.id == created.id))).scalar_one()
    await db_session.refresh(row)
    assert row.last_active_at.replace(tzinfo=timezone.utc) == later


class _S(TypedDict, total=False):
    n: int


def _write_checkpoint(saver, thread_id: str):
    graph = StateGraph(_S)
    graph.add_node("a", lambda state: {"n": 1})
    graph.add_edge(START, "a")
    graph.add_edge("a", END)
    return graph.compile(checkpointer=saver), {"configurable": {"thread_id": thread_id}}


async def test_pruning_removes_idle_conversations_and_their_memory_but_keeps_active_ones(db_session):
    provider = CheckpointerProvider()  # in-memory saver
    repo = ConversationRepository(db_session)
    user = uuid.uuid4()
    now = datetime.now(timezone.utc)
    old = await repo.open(None, user_id=user, household_id=HOME)
    recent = await repo.open(None, user_id=user, household_id=HOME)
    await repo.touch(old.id, now=now - timedelta(days=40))
    await repo.touch(recent.id, now=now - timedelta(days=2))

    graphs = {}
    for ref in (old, recent):
        graphs[ref.id] = _write_checkpoint(provider.saver, ref.thread_id)
        await graphs[ref.id][0].ainvoke({}, graphs[ref.id][1])
        assert (await graphs[ref.id][0].aget_state(graphs[ref.id][1])).values == {"n": 1}

    removed = await prune_conversations(db_session, provider, retention_days=30, now=now)

    assert removed == 1
    remaining = {r for r in (await db_session.execute(select(AssistantConversation.id))).scalars()}
    assert remaining == {recent.id}
    assert (await graphs[old.id][0].aget_state(graphs[old.id][1])).values == {}, "memory went with the row"
    assert (await graphs[recent.id][0].aget_state(graphs[recent.id][1])).values == {"n": 1}


async def test_pruning_is_a_no_op_when_nothing_is_idle(db_session):
    repo = ConversationRepository(db_session)
    await repo.open(None, user_id=uuid.uuid4(), household_id=HOME)
    assert await prune_conversations(db_session, CheckpointerProvider(), retention_days=30) == 0


def test_thread_id_for_a_row_matches_the_reference():
    row = AssistantConversation(id="a" * 32, user_id=uuid.uuid4(), household_id=HOME)
    assert thread_id_for(row) == f"chat:{row.user_id}:{HOME}:{'a' * 32}"
