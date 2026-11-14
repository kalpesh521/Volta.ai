"""
Transactional outbox for device commands (tracker 4.3 and 4.5).

The problem it fixes: the action graph used to publish to RabbitMQ *inside* the confirm request, before the database
commit. If the commit then failed, a command was on the wire for an action the database never recorded; if the broker
was down, a confirmed action was lost. Now:

1. **Accept** (`OutboxPublisher`, inside the request): the command is written to `device_command_outbox` in the same
   transaction as the proposal's `queued` status and its audit event. Both exist or neither does.
2. **Deliver** (`OutboxRelay`, after the commit): pending rows are published with retries and a growing pause. A
   crash between publishing and marking the row sent means the command is published again; that is safe because the
   message id is the command id and the consumer ignores a command that already ended (see `execution.py`).

What the relay refuses to do, whatever the row says:

* **Kill switch.** If device control is switched off at delivery time, nothing is sent and the proposal fails with an
  audit event. The switch is checked when delivering, not only when proposing, so flipping it stops queued commands.
* **Stale commands.** A row older than `DEVICE_COMMAND_MAX_AGE_SECONDS` is dropped: turning something on because of a
  confirmation given long ago, after an outage, is not what the user asked for.
* **Endless retries.** After `OUTBOX_MAX_ATTEMPTS` the row is `dead` and the user is told.

Rows are claimed with `FOR UPDATE SKIP LOCKED`, so several relays can run side by side without delivering one command twice
at the same moment.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.agents.device_action import states
from app.agents_platform.agents.device_action.models import ActionAuditEvent, ActionProposal, DeviceCommandOutbox
from app.agents_platform.agents.device_action.publisher import CommandBrokerError, CommandPublisher, DeviceCommand
from app.core.config import Settings
from app.core.metrics import ACTION_PROPOSALS, OUTBOX_DELAY, OUTBOX_DISPATCH, OUTBOX_PENDING
from app.modules.assistant.notifications.deliver import save_notification

logger = logging.getLogger("volta.actions.outbox")

PENDING, SENT, BLOCKED, EXPIRED_ROW, DEAD = "pending", "sent", "blocked", "expired", "dead"
_OUTCOME = {BLOCKED: "blocked", EXPIRED_ROW: "expired", DEAD: "dead"}  # row status -> metric label and result field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class OutboxPublisher:
    """
    The `CommandPublisher` the action graph is given when the outbox is on. It writes a row instead of calling the
    broker, so it cannot fail for broker reasons and joins the caller's transaction.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def publish(self, command: DeviceCommand) -> str:
        existing = await self._db.scalar(
            select(DeviceCommandOutbox.id).where(DeviceCommandOutbox.command_id == command.command_id)
        )
        if existing is None:  # the same command is never recorded twice
            self._db.add(
                DeviceCommandOutbox(
                    command_id=command.command_id,
                    household_id=command.household_id,
                    user_id=uuid.UUID(str(command.user_id)),
                    payload_json=json.dumps(
                        {
                            "command_id": command.command_id,
                            "household_id": command.household_id,
                            "user_id": command.user_id,
                            "device": command.device,
                            "command": command.command,
                            "duration_minutes": command.duration_minutes,
                            "power_kw": command.power_kw,
                        }
                    ),
                )
            )
            await self._db.flush()
        return command.command_id


@dataclass
class DispatchResult:
    sent: int = 0
    retried: int = 0
    dead: int = 0
    blocked: int = 0
    expired: int = 0
    command_ids: list[str] = field(default_factory=list)

    @property
    def handled(self) -> int:
        return self.sent + self.retried + self.dead + self.blocked + self.expired


class OutboxRelay:
    def __init__(
        self,
        transport: CommandPublisher,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._clock = clock

    async def dispatch(
        self, db: AsyncSession, *, command_ids: Sequence[str] | None = None, limit: int | None = None
    ) -> DispatchResult:
        """Deliver due rows (optionally only these commands). The caller commits."""
        now = self._clock()
        statement = (
            select(DeviceCommandOutbox)
            .where(DeviceCommandOutbox.status == PENDING, DeviceCommandOutbox.next_attempt_at <= now)
            .order_by(DeviceCommandOutbox.created_at)
            .limit(limit or self._settings.OUTBOX_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        if command_ids is not None:
            statement = statement.where(DeviceCommandOutbox.command_id.in_(list(command_ids)))
        rows = list((await db.execute(statement)).scalars())
        result = DispatchResult()
        for row in rows:
            await self._deliver(db, row, now, result)
        await db.flush()
        return result

    async def pending_count(self, db: AsyncSession) -> int:
        from sqlalchemy import func

        count = await db.scalar(
            select(func.count()).select_from(DeviceCommandOutbox).where(DeviceCommandOutbox.status == PENDING)
        )
        OUTBOX_PENDING.set(count or 0)
        return int(count or 0)

    async def _deliver(self, db: AsyncSession, row: DeviceCommandOutbox, now: datetime, result: DispatchResult) -> None:
        settings = self._settings
        if settings.DEVICE_KILL_SWITCH or not settings.DEVICE_CONTROL_ENABLED:
            await self._end(db, row, BLOCKED, "kill_switch", now, result, notify="Device control was switched off, so the command was not sent.")
            return
        if (now - _aware(row.created_at)).total_seconds() > settings.DEVICE_COMMAND_MAX_AGE_SECONDS:
            await self._end(db, row, EXPIRED_ROW, "too_old", now, result, notify="The command waited too long to be sent, so it was dropped.")
            return
        try:
            command = DeviceCommand(**json.loads(row.payload_json))
        except (ValueError, TypeError):  # not JSON, or not a command: no retry can fix it, and it must not block the queue
            await self._end(db, row, DEAD, "corrupt_payload", now, result, notify="The command could not be read, so it was not sent.")
            return
        try:
            await self._transport.publish(command)
        except CommandBrokerError as exc:
            row.attempts += 1
            row.last_error = str(exc)[:200]
            if row.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
                await self._end(db, row, DEAD, "broker_unavailable", now, result, notify="The command could not be delivered to your device.")
                return
            row.next_attempt_at = now + timedelta(seconds=settings.OUTBOX_BACKOFF_SECONDS * 2 ** (row.attempts - 1))
            OUTBOX_DISPATCH.labels(outcome="retry").inc()
            result.retried += 1
            logger.warning("outbox retry command_id=%s attempt=%d: %s", row.command_id, row.attempts, exc)
            return
        row.attempts += 1
        row.status = SENT
        row.sent_at = now
        row.last_error = None
        OUTBOX_DISPATCH.labels(outcome="sent").inc()
        OUTBOX_DELAY.observe(max(0.0, (now - _aware(row.created_at)).total_seconds()))
        result.sent += 1
        result.command_ids.append(row.command_id)
        await self._audit(db, row, "published")

    async def _end(
        self,
        db: AsyncSession,
        row: DeviceCommandOutbox,
        status: str,
        reason: str,
        now: datetime,
        result: DispatchResult,
        *,
        notify: str,
    ) -> None:
        row.status = status
        row.last_error = reason
        outcome = _OUTCOME[status]
        OUTBOX_DISPATCH.labels(outcome=outcome).inc()
        setattr(result, outcome, getattr(result, outcome) + 1)
        proposal = (
            await db.execute(select(ActionProposal).where(ActionProposal.command_id == row.command_id))
        ).scalar_one_or_none()
        if proposal is not None and states.can_transition(proposal.status, states.FAILED):
            proposal.status = states.FAILED
            proposal.updated_at = now
            ACTION_PROPOSALS.labels(status="failed").inc()
            await self._audit(db, row, "failed", proposal=proposal, reason=reason)
            await save_notification(
                db,
                user_id=proposal.user_id,
                household_id=proposal.household_id,
                kind="device_command_failed",
                title="Device command not sent",
                body=notify,
                ref_id=proposal.id,
            )
        logger.warning("outbox ended command_id=%s status=%s reason=%s", row.command_id, status, reason)

    async def _audit(
        self, db: AsyncSession, row: DeviceCommandOutbox, event: str, *, proposal: ActionProposal | None = None, reason: str | None = None
    ) -> None:
        proposal = proposal or (
            await db.execute(select(ActionProposal).where(ActionProposal.command_id == row.command_id))
        ).scalar_one_or_none()
        if proposal is None:
            return
        db.add(
            ActionAuditEvent(
                proposal_id=proposal.id,
                user_id=proposal.user_id,
                household_id=proposal.household_id,
                event=event,
                reasons_json=json.dumps([reason] if reason else []),
                checks_json=proposal.checks_json,
                command_id=row.command_id,
            )
        )


async def dispatch_now(relay: OutboxRelay, scope, command_id: str) -> None:
    """
    Deliver one just-confirmed command right after the response, instead of waiting for the next poll.

    Best effort: the row is already durable, so if this fails (or the process dies first) the poller delivers it.
    """
    try:
        async with scope() as db:
            await relay.dispatch(db, command_ids=[command_id])
    except Exception:  # noqa: BLE001 - the poller is the safety net, and a failure here must not reach the client
        logger.exception("immediate outbox dispatch failed for %s; the poller will retry", command_id)
