"""
The device-command outbox and the proposal state machine (tracker 4.3 and 4.5).

The property that matters: a command is on the wire only if the database says it should be, and a broker outage or a
crash loses nothing and duplicates nothing that matters.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.agents_platform.agents.device_action import states
from app.agents_platform.agents.device_action.deps import get_command_publisher
from app.agents_platform.agents.device_action.execution import record_virtual_result
from app.agents_platform.agents.device_action.models import (
    ActionAuditEvent,
    ActionProposal,
    DeviceCommandOutbox,
    DeviceOverride,
)
from app.agents_platform.agents.device_action.outbox import (
    DEAD,
    PENDING,
    SENT,
    OutboxPublisher,
    OutboxRelay,
    dispatch_now,
)
from app.agents_platform.agents.device_action.outbox_relay import run_forever
from app.agents_platform.agents.device_action.publisher import CommandBrokerError, DeviceCommand, RecordingPublisher
from app.core.config import Settings, settings as app_settings
from app.core.metrics import OUTBOX_DISPATCH
from app.models.user import User
from app.modules.assistant.notifications.models import HomeNotification
from main import app
from tests.assistant_helpers import TZ, auth_headers, ingest, make_tick
from tests.onboarding_helpers import complete_hybrid_onboarding
from tests.test_action_confirmation import PROPOSE, _confirm
from tests.test_safe_actions import HEATER

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)


def cfg(**overrides) -> Settings:
    base = dict(DEVICE_CONTROL_ENABLED=True, DEVICE_KILL_SWITCH=False, OUTBOX_MAX_ATTEMPTS=3, OUTBOX_BACKOFF_SECONDS=2.0,
                DEVICE_COMMAND_MAX_AGE_SECONDS=300, OUTBOX_BATCH_SIZE=20)
    return Settings(**{**base, **overrides})


def command(command_id: str | None = None, user_id: uuid.UUID | None = None) -> DeviceCommand:
    return DeviceCommand(
        command_id=command_id or uuid.uuid4().hex, household_id="home_001", user_id=str(user_id or uuid.uuid4()),
        device="water_heater", command="on", duration_minutes=30, power_kw=2.0,
    )


class FlakyTransport:
    """Fails the first `failures` publishes with a broker error, then works. Records what reached the wire."""

    def __init__(self, failures: int = 0) -> None:
        self.failures, self.sent, self.attempts = failures, [], 0

    async def publish(self, cmd: DeviceCommand) -> str:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise CommandBrokerError("RabbitMQ is unavailable")
        self.sent.append(cmd.command_id)
        return cmd.command_id


async def make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", name="T", hashed_password=None)
    db.add(user)
    await db.flush()
    return user


async def queued_proposal(db, user: User, cmd: DeviceCommand, status: str = states.QUEUED) -> ActionProposal:
    proposal = ActionProposal(
        id=uuid.uuid4().hex, user_id=user.id, household_id=cmd.household_id, question="q", device=cmd.device,
        command=cmd.command, status=status, command_id=cmd.command_id,
    )
    db.add(proposal)
    await db.flush()
    return proposal


async def accepted(db, transport, *, user=None, status=states.QUEUED, **cfg_overrides):
    """A command accepted into the outbox with its proposal, and a relay ready to deliver it."""
    user = user or await make_user(db)
    cmd = command(user_id=user.id)
    proposal = await queued_proposal(db, user, cmd, status)
    await OutboxPublisher(db).publish(cmd)
    row = await row_of(db, cmd.command_id)
    row.created_at = row.next_attempt_at = NOW  # the relay below runs on the same fixed clock
    await db.flush()
    return OutboxRelay(transport, cfg(**cfg_overrides), clock=lambda: NOW), cmd, proposal


async def row_of(db, command_id) -> DeviceCommandOutbox:
    return (await db.execute(select(DeviceCommandOutbox).where(DeviceCommandOutbox.command_id == command_id))).scalar_one()


async def events(db, proposal_id) -> list[str]:
    rows = (await db.execute(select(ActionAuditEvent).where(ActionAuditEvent.proposal_id == proposal_id).order_by(ActionAuditEvent.created_at))).scalars()
    return [r.event for r in rows]


def count(outcome: str) -> float:
    return OUTBOX_DISPATCH.labels(outcome=outcome)._value.get()


# --- accepting a command --------------------------------------------------------------------------


async def test_accepting_a_command_writes_a_row_and_nothing_reaches_the_broker(db_session):
    transport = FlakyTransport()
    cmd = command()
    assert await OutboxPublisher(db_session).publish(cmd) == cmd.command_id

    row = await row_of(db_session, cmd.command_id)
    assert row.status == PENDING and row.attempts == 0 and json.loads(row.payload_json)["device"] == "water_heater"
    assert transport.sent == []


async def test_the_same_command_is_never_recorded_twice(db_session):
    cmd = command()
    for _ in range(3):
        await OutboxPublisher(db_session).publish(cmd)
    assert await db_session.scalar(select(func.count()).select_from(DeviceCommandOutbox)) == 1


async def test_a_rolled_back_transaction_leaves_no_outbox_row_so_nothing_can_be_published_for_it(db_session):
    transport = FlakyTransport()
    cmd = command()
    async with db_session.begin_nested() as savepoint:
        await OutboxPublisher(db_session).publish(cmd)
        await savepoint.rollback()  # the request failed after the graph "published"

    assert await db_session.scalar(select(func.count()).select_from(DeviceCommandOutbox)) == 0
    result = await OutboxRelay(transport, cfg(), clock=lambda: NOW).dispatch(db_session)
    assert result.handled == 0 and transport.sent == [], "this is the publish-before-commit gap, closed"


# --- delivering -----------------------------------------------------------------------------------


async def test_a_pending_command_is_delivered_once_and_marked_sent_with_an_audit_event(db_session):
    transport = FlakyTransport()
    relay, cmd, proposal = await accepted(db_session, transport)
    before = count("sent")

    result = await relay.dispatch(db_session)

    assert result.sent == 1 and transport.sent == [cmd.command_id]
    row = await row_of(db_session, cmd.command_id)
    assert row.status == SENT and row.attempts == 1 and row.sent_at is not None
    assert await events(db_session, proposal.id) == ["published"]
    assert count("sent") == before + 1
    assert (await relay.dispatch(db_session)).handled == 0 and transport.sent == [cmd.command_id], "never delivered twice"


async def test_a_broker_outage_loses_nothing_the_command_waits_and_goes_out_when_the_broker_returns(db_session):
    transport = FlakyTransport(failures=2)
    relay, cmd, proposal = await accepted(db_session, transport)
    retried = count("retry")

    first = await relay.dispatch(db_session)
    row = await row_of(db_session, cmd.command_id)
    assert first.retried == 1 and row.status == PENDING and row.attempts == 1
    assert row.next_attempt_at == NOW + timedelta(seconds=2.0) and "unavailable" in row.last_error
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.QUEUED, "the user's action is still alive"

    assert (await relay.dispatch(db_session)).handled == 0, "not due yet: the pause is respected"
    later = OutboxRelay(transport, cfg(), clock=lambda: NOW + timedelta(seconds=2.1))
    assert (await later.dispatch(db_session)).retried == 1
    assert (await row_of(db_session, cmd.command_id)).next_attempt_at == NOW + timedelta(seconds=2.1) + timedelta(seconds=4.0)
    final = OutboxRelay(transport, cfg(), clock=lambda: NOW + timedelta(seconds=10))
    assert (await final.dispatch(db_session)).sent == 1 and transport.sent == [cmd.command_id]
    assert count("retry") == retried + 2


async def test_after_the_last_attempt_the_command_is_dead_the_proposal_fails_and_the_user_is_told(db_session):
    transport = FlakyTransport(failures=99)
    relay, cmd, proposal = await accepted(db_session, transport, OUTBOX_MAX_ATTEMPTS=2)
    dead = count("dead")

    await relay.dispatch(db_session)
    result = await OutboxRelay(transport, cfg(OUTBOX_MAX_ATTEMPTS=2), clock=lambda: NOW + timedelta(minutes=1)).dispatch(db_session)

    assert result.dead == 1 and (await row_of(db_session, cmd.command_id)).status == DEAD
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.FAILED
    assert (await events(db_session, proposal.id))[-1] == "failed"
    note = (await db_session.execute(select(HomeNotification).where(HomeNotification.ref_id == proposal.id))).scalar_one()
    assert note.kind == "device_command_failed" and count("dead") == dead + 1


@pytest.mark.parametrize("overrides", [{"DEVICE_KILL_SWITCH": True}, {"DEVICE_CONTROL_ENABLED": False}])
async def test_the_kill_switch_is_checked_at_delivery_so_flipping_it_stops_commands_already_queued(db_session, overrides):
    transport = FlakyTransport()
    relay, cmd, proposal = await accepted(db_session, transport, **overrides)
    blocked = count("blocked")

    result = await relay.dispatch(db_session)

    assert result.blocked == 1 and transport.sent == [] and transport.attempts == 0
    assert (await row_of(db_session, cmd.command_id)).status == "blocked"
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.FAILED
    assert (await events(db_session, proposal.id))[-1] == "failed" and count("blocked") == blocked + 1


async def test_a_command_that_waited_too_long_is_dropped_not_sent_late(db_session):
    transport = FlakyTransport()
    relay, cmd, proposal = await accepted(db_session, transport, DEVICE_COMMAND_MAX_AGE_SECONDS=300)
    row = await row_of(db_session, cmd.command_id)
    row.created_at = NOW - timedelta(seconds=301)
    await db_session.flush()

    result = await relay.dispatch(db_session)

    assert result.expired == 1 and transport.sent == []
    assert (await row_of(db_session, cmd.command_id)).status == "expired"
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.FAILED


async def test_a_command_just_inside_the_age_limit_is_still_sent(db_session):
    transport = FlakyTransport()
    relay, cmd, _ = await accepted(db_session, transport)
    row = await row_of(db_session, cmd.command_id)
    row.created_at = NOW - timedelta(seconds=299)
    await db_session.flush()
    assert (await relay.dispatch(db_session)).sent == 1


async def test_a_proposal_that_already_ended_is_not_flipped_to_failed(db_session):
    transport = FlakyTransport()
    relay, cmd, proposal = await accepted(db_session, transport, status=states.APPLIED, DEVICE_KILL_SWITCH=True)
    await relay.dispatch(db_session)
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.APPLIED


async def test_only_the_asked_for_commands_are_delivered_and_the_batch_is_bounded(db_session):
    transport = FlakyTransport()
    user = await make_user(db_session)
    relay, first, _ = await accepted(db_session, transport, user=user)
    _, second, _ = await accepted(db_session, transport, user=user)
    _, third, _ = await accepted(db_session, transport, user=user)

    only = await relay.dispatch(db_session, command_ids=[second.command_id])
    assert only.command_ids == [second.command_id]
    limited = await relay.dispatch(db_session, limit=1)
    assert limited.sent == 1 and len(transport.sent) == 2
    assert (await relay.dispatch(db_session)).sent == 1 and set(transport.sent) == {first.command_id, second.command_id, third.command_id}


async def test_pending_count_reports_what_is_waiting(db_session):
    from app.core.metrics import OUTBOX_PENDING

    relay, _, _ = await accepted(db_session, FlakyTransport())
    assert await relay.pending_count(db_session) == 1 and OUTBOX_PENDING._value.get() == 1


async def test_a_corrupt_row_is_marked_dead_and_cannot_stop_the_rest_from_being_delivered(db_session):
    transport = FlakyTransport()
    user = await make_user(db_session)
    relay, good, _ = await accepted(db_session, transport, user=user)
    bad_cmd = command(user_id=user.id)
    bad_proposal = await queued_proposal(db_session, user, bad_cmd)
    await OutboxPublisher(db_session).publish(bad_cmd)
    bad = await row_of(db_session, bad_cmd.command_id)
    bad.payload_json = "{not json"
    bad.created_at = bad.next_attempt_at = NOW - timedelta(seconds=10)  # claimed first
    await db_session.flush()

    result = await relay.dispatch(db_session)

    assert result.dead == 1 and result.sent == 1 and transport.sent == [good.command_id]
    assert (await row_of(db_session, bad_cmd.command_id)).last_error == "corrupt_payload"
    assert (await db_session.get(ActionProposal, bad_proposal.id)).status == states.FAILED


# --- the consumer is idempotent -------------------------------------------------------------------


async def test_a_command_delivered_twice_is_applied_once(db_session):
    user = await make_user(db_session)
    cmd = command(user_id=user.id)
    proposal = await queued_proposal(db_session, user, cmd)
    payload = {"command_id": cmd.command_id, "household_id": cmd.household_id, "user_id": cmd.user_id, "device": "water_heater",
               "command": "on", "duration_minutes": 30, "power_kw": 2.0}

    first = await record_virtual_result(db_session, payload)
    second = await record_virtual_result(db_session, payload)

    assert first["applied"] is True and second == {"applied": True, "status": "duplicate", "command_id": cmd.command_id}
    assert await db_session.scalar(select(func.count()).select_from(DeviceOverride).where(DeviceOverride.command_id == cmd.command_id)) == 1
    assert (await events(db_session, proposal.id)).count("applied") == 1
    assert (await db_session.get(ActionProposal, proposal.id)).status == states.APPLIED


# --- the state machine ----------------------------------------------------------------------------


def test_a_terminal_state_never_changes_again():
    for state in states.TERMINAL:
        assert states.TRANSITIONS[state] == frozenset()
        for target in states.TRANSITIONS:
            assert not states.can_transition(state, target)
    assert {states.APPLIED, states.FAILED, states.CANCELLED, states.REJECTED, states.EXPIRED} == set(states.TERMINAL)


def test_every_state_can_reach_an_end_so_no_proposal_is_stuck_forever():
    def reaches_terminal(state: str, seen: frozenset[str] = frozenset()) -> bool:
        if state in states.TERMINAL:
            return True
        return any(reaches_terminal(n, seen | {state}) for n in states.TRANSITIONS[state] if n not in seen)

    assert all(reaches_terminal(s) for s in states.TRANSITIONS)


def test_the_normal_path_is_legal_and_shortcuts_are_not():
    path = [states.AWAITING, states.CONFIRMING, states.QUEUED, states.APPLIED]
    for a, b in zip(path, path[1:]):
        states.require_transition(a, b)
    for a, b in [(states.AWAITING, states.APPLIED), (states.AWAITING, states.QUEUED), (states.APPLIED, states.FAILED), (states.FAILED, states.APPLIED), (states.EXPIRED, states.CONFIRMING)]:
        with pytest.raises(states.IllegalTransitionError):
            states.require_transition(a, b)
    assert not states.can_transition("nonsense", states.APPLIED)


# --- the poller -----------------------------------------------------------------------------------


async def test_the_poller_keeps_going_after_a_failed_pass_and_stops_on_request(monkeypatch):
    import asyncio

    from app.agents_platform.agents.device_action import outbox_relay

    passes = []

    async def fake_once(relay):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("database blip")
        if len(passes) >= 3:
            stop.set()
        return 0

    stop = asyncio.Event()
    monkeypatch.setattr(outbox_relay, "run_once", fake_once)
    await asyncio.wait_for(run_forever(object(), stop, poll_seconds=0.01), timeout=2)
    assert len(passes) == 3, "one failed pass did not end the process"


# --- over HTTP ------------------------------------------------------------------------------------


@pytest.fixture
def outbox_on(monkeypatch, db_scope):
    monkeypatch.setattr(app_settings, "DEVICE_COMMAND_OUTBOX_ENABLED", True)
    monkeypatch.setattr(app_settings, "DEVICE_CONTROL_ENABLED", True)
    monkeypatch.setattr(app_settings, "DEVICE_KILL_SWITCH", False)
    yield


async def confirmed_proposal(client, email: str, transport) -> tuple[dict, dict]:
    app.dependency_overrides[get_command_publisher] = lambda: transport
    headers = await auth_headers(client, email)
    household_id = await complete_hybrid_onboarding(client, headers)
    await ingest(client, make_tick(datetime.now(TZ).replace(microsecond=0), household_id=household_id, soc=70, solar_kw=3.2, load_kw=0.8, devices=[HEATER]))
    proposed = (await client.post(PROPOSE, json={"question": "Run the water heater now."}, headers=headers)).json()
    assert proposed["status"] == "awaiting_confirmation"
    return headers, proposed


async def test_confirming_records_the_command_and_delivers_it_straight_after(client, db_session, outbox_on):
    transport = RecordingPublisher()
    headers, proposed = await confirmed_proposal(client, "outbox-ok@example.com", transport)

    response = await client.post(_confirm(proposed["proposal_id"]), json={"confirmed": True}, headers=headers)

    body = response.json()
    assert response.status_code == 200 and body["status"] == "queued" and body["command_id"]
    assert [c.command_id for c in transport.commands] == [body["command_id"]], "delivered once, right after the response"
    assert (await row_of(db_session, body["command_id"])).status == SENT
    assert "published" in await events(db_session, proposed["proposal_id"])


async def test_with_the_broker_down_the_user_still_gets_queued_and_the_command_is_not_lost(client, db_session, outbox_on):
    transport = FlakyTransport(failures=1)
    headers, proposed = await confirmed_proposal(client, "outbox-down@example.com", transport)

    response = await client.post(_confirm(proposed["proposal_id"]), json={"confirmed": True}, headers=headers)

    body = response.json()
    assert response.status_code == 200 and body["status"] == "queued", "before the outbox this was 'failed: broker_unavailable'"
    row = await row_of(db_session, body["command_id"])
    assert row.status == PENDING and row.attempts == 1 and transport.sent == []

    later = OutboxRelay(transport, cfg(), clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=30))
    assert (await later.dispatch(db_session)).sent == 1 and transport.sent == [body["command_id"]]


async def test_the_inline_path_still_works_when_the_outbox_is_switched_off(client, monkeypatch, db_scope):
    monkeypatch.setattr(app_settings, "DEVICE_COMMAND_OUTBOX_ENABLED", False)
    monkeypatch.setattr(app_settings, "DEVICE_CONTROL_ENABLED", True)
    monkeypatch.setattr(app_settings, "DEVICE_KILL_SWITCH", False)
    transport = RecordingPublisher()
    headers, proposed = await confirmed_proposal(client, "outbox-off@example.com", transport)

    body = (await client.post(_confirm(proposed["proposal_id"]), json={"confirmed": True}, headers=headers)).json()

    assert body["status"] == "queued" and [c.command_id for c in transport.commands] == [body["command_id"]]


async def test_dispatch_now_never_raises_because_the_poller_is_the_safety_net():
    class Boom:
        async def dispatch(self, db, **kw):
            raise RuntimeError("down")

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        yield object()

    await dispatch_now(Boom(), scope, "abc")  # must not raise
