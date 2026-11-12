"""Confirmation lifecycle: deadline, single use, replay protection, and the maintenance sweep."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from typing_extensions import TypedDict

from app.ai.checkpointer import CheckpointerProvider, libpq_conninfo
from app.ai.config import AISettings
from app.core.config import settings
from app.agents_platform.agents.device_action.deps import get_action_graph, get_command_publisher
from app.agents_platform.agents.device_action.maintenance import expire_waiting, prune_checkpoints, sweep
from app.agents_platform.agents.device_action.models import ActionAuditEvent, ActionProposal
from app.agents_platform.agents.device_action.publisher import RecordingPublisher
from app.agents_platform.agents.device_action.service import AWAITING, CONFIRMING, ActionService
from main import app
from tests.assistant_helpers import TZ, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_safe_actions import HEATER

PROPOSE = "/assistant/actions/propose"


@pytest.fixture
def publisher(monkeypatch) -> RecordingPublisher:
    recorder = RecordingPublisher()
    app.dependency_overrides[get_command_publisher] = lambda: recorder
    monkeypatch.setattr(settings, "DEVICE_CONTROL_ENABLED", True)
    monkeypatch.setattr(settings, "DEVICE_KILL_SWITCH", False)
    yield recorder
    app.dependency_overrides.pop(get_command_publisher, None)


async def _proposal(client, email: str) -> tuple[dict[str, str], dict]:
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(client, headers)
    now = datetime.now(TZ).replace(microsecond=0)
    await ingest(
        client,
        make_tick(now, household_id=household_id, soc=70, solar_kw=3.2, load_kw=0.8, devices=[HEATER]),
    )
    response = await client.post(PROPOSE, json={"question": "Run the water heater now."}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "awaiting_confirmation"
    return headers, body


def _confirm(proposal_id: str) -> str:
    return f"/assistant/actions/{proposal_id}/confirm"


async def test_proposal_carries_a_confirmation_deadline(client, publisher):
    before = datetime.now(timezone.utc)
    _, body = await _proposal(client, "deadline@example.com")

    expires_at = datetime.fromisoformat(body["expires_at"])
    expected = before + timedelta(seconds=settings.ACTION_CONFIRMATION_TTL_SECONDS)
    assert abs((expires_at - expected).total_seconds()) < 30


async def test_expired_proposal_cannot_be_confirmed(client, db_session, publisher):
    headers, body = await _proposal(client, "expired@example.com")
    await db_session.execute(
        update(ActionProposal)
        .where(ActionProposal.id == body["proposal_id"])
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )

    response = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "proposal_expired"
    assert publisher.commands == []

    shown = await client.get(f"/assistant/actions/{body['proposal_id']}", headers=headers)
    assert shown.json()["status"] == "expired"
    assert shown.json()["expires_at"] is None


async def test_confirmation_is_single_use(client, publisher):
    headers, body = await _proposal(client, "single-use@example.com")

    first = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)
    second = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)

    assert first.status_code == 200 and first.json()["status"] == "queued"
    assert second.status_code == 409
    assert len(publisher.commands) == 1


async def test_replay_is_blocked_even_if_the_row_reverts(client, db_session, publisher):
    """If the request that confirmed an action failed to commit, the row goes back to
    'awaiting'. The finished graph thread must still refuse a second publish."""
    headers, body = await _proposal(client, "replay@example.com")
    first = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)
    assert first.status_code == 200
    await db_session.execute(
        update(ActionProposal)
        .where(ActionProposal.id == body["proposal_id"])
        .values(status=AWAITING, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    )

    replay = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)

    assert replay.status_code == 409
    assert len(publisher.commands) == 1


async def test_declining_a_proposal_sends_nothing(client, publisher):
    headers, body = await _proposal(client, "decline@example.com")

    response = await client.post(_confirm(body["proposal_id"]), json={"confirmed": False}, headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert publisher.commands == []


async def test_claim_moves_a_proposal_out_of_awaiting_exactly_once(db_session):
    proposal_id = uuid.uuid4().hex
    db_session.add(
        ActionProposal(
            id=proposal_id,
            user_id=uuid.uuid4(),
            household_id="home_x",
            question="turn on the water heater",
            status=AWAITING,
        )
    )
    await db_session.flush()
    service = ActionService(db=db_session, energy=None, graph=None, publisher=None)  # type: ignore[arg-type]

    assert await service._claim(proposal_id) is True
    assert await service._claim(proposal_id) is False

    status = (await db_session.execute(select(ActionProposal.status).where(ActionProposal.id == proposal_id))).scalar_one()
    assert status == CONFIRMING


class _Trivial(TypedDict, total=False):
    value: int


async def _save_checkpoint(provider: CheckpointerProvider, thread_id: str) -> None:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(_Trivial)
    graph.add_node("set", lambda state: {"value": 1})
    graph.add_edge(START, "set")
    graph.add_edge("set", END)
    await graph.compile(checkpointer=provider.saver).ainvoke({}, {"configurable": {"thread_id": thread_id}})


async def _has_checkpoint(provider: CheckpointerProvider, thread_id: str) -> bool:
    return await provider.saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None


def _row(status: str, *, updated_at: datetime, expires_at: datetime | None = None) -> ActionProposal:
    return ActionProposal(
        id=uuid.uuid4().hex,
        user_id=uuid.uuid4(),
        household_id="home_x",
        question="turn on the water heater",
        status=status,
        expires_at=expires_at,
        updated_at=updated_at,
    )


async def test_sweep_expires_waiting_proposals_and_audits_them(db_session):
    now = datetime.now(timezone.utc)
    stale = _row(AWAITING, updated_at=now, expires_at=now - timedelta(minutes=1))
    fresh = _row(AWAITING, updated_at=now, expires_at=now + timedelta(minutes=1))
    db_session.add_all([stale, fresh])
    await db_session.flush()

    assert await expire_waiting(db_session, now=now) == 1

    assert stale.status == "expired" and stale.expires_at is None
    assert fresh.status == AWAITING
    events = (await db_session.execute(select(ActionAuditEvent.event).where(ActionAuditEvent.proposal_id == stale.id))).scalars().all()
    assert events == ["expired"]


async def test_sweep_prunes_only_checkpoints_that_ended_inside_the_window(db_session):
    provider = CheckpointerProvider()
    await provider.start()
    now = datetime.now(timezone.utc)
    hours = 24
    due = _row("applied", updated_at=now - timedelta(hours=hours + 1))
    recent = _row("applied", updated_at=now - timedelta(hours=1))
    open_ = _row(AWAITING, updated_at=now - timedelta(hours=hours + 1), expires_at=now + timedelta(minutes=1))
    db_session.add_all([due, recent, open_])
    await db_session.flush()
    for row in (due, recent, open_):
        await _save_checkpoint(provider, row.id)

    pruned = await prune_checkpoints(db_session, provider, now=now, retention_hours=hours)

    assert pruned == 1
    assert not await _has_checkpoint(provider, due.id)
    assert await _has_checkpoint(provider, recent.id)
    assert await _has_checkpoint(provider, open_.id)
    await provider.stop()


async def test_sweep_reports_both_counts(db_session):
    provider = CheckpointerProvider()
    await provider.start()
    now = datetime.now(timezone.utc)
    db_session.add(_row(AWAITING, updated_at=now, expires_at=now - timedelta(seconds=5)))
    await db_session.flush()

    result = await sweep(db_session, provider, retention_hours=24, now=now)

    assert (result.expired, result.pruned_threads) == (1, 0)
    await provider.stop()


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


@asynccontextmanager
async def _postgres_checkpoints():
    """Route the action graph's checkpoints to a throwaway schema in a real Postgres."""
    import psycopg

    from app.ai import checkpointer as module

    schema = f"lg_test_{uuid.uuid4().hex[:8]}"
    provider = CheckpointerProvider()
    await provider.start(
        AISettings(AI_CHECKPOINT_BACKEND="postgres", AI_CHECKPOINT_DATABASE_URL=POSTGRES_URL, AI_CHECKPOINT_SCHEMA=schema),
        auto_setup=True,
    )
    original = module.checkpointer_provider._saver
    module.checkpointer_provider._saver = provider.saver
    get_action_graph.cache_clear()
    try:
        yield provider
    finally:
        module.checkpointer_provider._saver = original
        get_action_graph.cache_clear()
        await provider.stop()
        with psycopg.connect(libpq_conninfo(POSTGRES_URL), autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")
async def test_the_whole_flow_works_on_postgres_checkpoints(client, publisher):
    """Propose, confirm and replay with the real saver: state must serialise, resume and finish.

    The pool is opened inside the test so it lives on the test's own event loop."""
    async with _postgres_checkpoints() as checkpoints:
        headers, body = await _proposal(client, "pg-flow@example.com")
        thread = {"configurable": {"thread_id": body["proposal_id"]}}
        assert await checkpoints.saver.aget_tuple(thread) is not None  # the pause is in Postgres, not memory

        first = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)
        replay = await client.post(_confirm(body["proposal_id"]), json={"confirmed": True}, headers=headers)

    assert first.status_code == 200 and first.json()["status"] == "queued"
    assert replay.status_code == 409
    assert len(publisher.commands) == 1
