"""Propose a device action, pause for confirmation, then queue it."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import AppError, ForbiddenError
from app.core.metrics import ACTION_PROPOSALS
from app.agents_platform.agents.device_action.graph import ActionRuntime
from app.agents_platform.agents.device_action.models import ActionAuditEvent, ActionProposal, DeviceOverride
from app.modules.assistant.notifications.deliver import save_notification
from app.agents_platform.agents.device_action.outbox import OutboxPublisher
from app.agents_platform.agents.device_action.publisher import CommandPublisher
from app.agents_platform.agents.device_action.safety import explain
from app.agents_platform.agents.device_action.schemas import ActionResponse
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger("volta.actions")

# `confirming` is a short-lived claim held while the graph resumes. It makes a proposal
# single-use: of two concurrent confirmations only one can move the row out of AWAITING.
AWAITING = "awaiting_confirmation"
CONFIRMING = "confirming"


class ProposalExpiredError(AppError):
    status_code = 409
    code = "proposal_expired"

    def __init__(self, message: str = "This confirmation expired. Ask again.") -> None:
        super().__init__(message)


class ActionService:
    def __init__(
        self,
        *,
        db: AsyncSession,
        energy: EnergyService,
        graph: CompiledStateGraph,
        publisher: CommandPublisher,
    ) -> None:
        self._db = db
        self._energy = energy
        self._graph = graph
        self._publisher = publisher

    async def propose(self, system: SolarSystem, user_id: uuid.UUID, question: str) -> ActionResponse:
        if system.user_id != user_id:
            raise ForbiddenError("This home is not yours.")
        proposal_id = uuid.uuid4().hex
        state = await self._invoke(
            system,
            {"proposal_id": proposal_id, "question": question, "household_id": system.household_id, "user_id": str(user_id)},
            proposal_id,
        )
        expires_at = _utcnow() + timedelta(seconds=settings.ACTION_CONFIRMATION_TTL_SECONDS)
        response = _response(proposal_id, state, expires_at=expires_at)
        await self._save(
            response,
            user_id=user_id,
            household_id=system.household_id,
            question=question,
            expires_at=expires_at if response.status == AWAITING else None,
        )
        await self._audit(response, user_id=user_id, household_id=system.household_id, event=_audit_event(response.status))
        ACTION_PROPOSALS.labels(status=response.status).inc()
        if response.status == AWAITING:
            await self._notify_waiting(user_id, system.household_id, proposal_id, response)
        logger.info(
            "action propose id=%s household=%s status=%s device=%s command=%s reasons=%s",
            proposal_id,
            system.household_id,
            response.status,
            response.device,
            response.command,
            ",".join(response.reasons),
        )
        return response

    async def confirm(self, system: SolarSystem, user_id: uuid.UUID, proposal_id: str, confirmed: bool) -> ActionResponse:
        return await self._resume(system, user_id, proposal_id, confirmed, rejected=False)

    async def reject(self, system: SolarSystem, user_id: uuid.UUID, proposal_id: str) -> ActionResponse:
        return await self._resume(system, user_id, proposal_id, False, rejected=True)

    async def cancel(self, system: SolarSystem, user_id: uuid.UUID, proposal_id: str) -> ActionResponse:
        proposal = await self._owned(proposal_id, user_id, system.household_id)
        if proposal.status == "queued":
            proposal.status = "cancelled"
            proposal.updated_at = _utcnow()
            await self._audit_row(proposal, event="cancelled")
            await self._db.flush()
            return await self.get(system, user_id, proposal_id)
        if proposal.status != AWAITING:
            raise ProposalExpiredError("This action can no longer be cancelled.")
        return await self._resume(system, user_id, proposal_id, False, rejected=False)

    async def get(self, system: SolarSystem, user_id: uuid.UUID, proposal_id: str) -> ActionResponse:
        proposal = await self._owned(proposal_id, user_id, system.household_id)
        return _from_row(proposal, await self._events(proposal_id))

    async def list_for_home(self, system: SolarSystem, user_id: uuid.UUID) -> list[ActionResponse]:
        result = await self._db.execute(
            select(ActionProposal)
            .where(ActionProposal.user_id == user_id, ActionProposal.household_id == system.household_id)
            .order_by(ActionProposal.created_at.desc())
            .limit(20)
        )
        return [_from_row(row, []) for row in result.scalars()]

    async def _resume(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        proposal_id: str,
        confirmed: bool,
        *,
        rejected: bool,
    ) -> ActionResponse:
        proposal = await self._owned(proposal_id, user_id, system.household_id)
        if proposal.status != AWAITING:
            raise ProposalExpiredError("This action is no longer waiting for confirmation.")
        if _is_expired(proposal.expires_at):
            raise ProposalExpiredError()
        # A finished thread has no pending step, so a second resume can never publish again,
        # even if the database row was rolled back after the first one.
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": proposal_id}})
        if snapshot is None or not snapshot.values or not snapshot.next:
            raise ProposalExpiredError()
        if not await self._claim(proposal_id):
            raise ProposalExpiredError("This action is no longer waiting for confirmation.")
        await self._db.refresh(proposal)
        state = await self._invoke(system, Command(resume={"confirmed": confirmed}), proposal_id)
        response = _response(proposal_id, state)
        ACTION_PROPOSALS.labels(status=response.status).inc()
        if rejected and response.status == "cancelled":
            response = response.model_copy(update={"status": "rejected", "message": explain("rejected", response.reasons, response.device, response.command)})
        proposal.status = response.status
        proposal.reasons_json = json.dumps(response.reasons)
        proposal.checks_json = json.dumps(response.checks)
        proposal.command_id = response.command_id
        proposal.device = response.device
        proposal.command = response.command
        proposal.duration_minutes = state.get("duration_minutes")
        proposal.expires_at = None
        proposal.updated_at = _utcnow()
        if confirmed and response.status == "queued":
            await self._audit(response, user_id=user_id, household_id=system.household_id, event="confirmed")
        event = "rejected" if rejected else _audit_event(response.status)
        await self._audit(response, user_id=user_id, household_id=system.household_id, event=event)
        await self._db.flush()
        logger.info(
            "action confirm id=%s status=%s command_id=%s reasons=%s",
            proposal_id,
            response.status,
            response.command_id,
            ",".join(response.reasons),
        )
        return response

    async def _invoke(self, system: SolarSystem, payload, proposal_id: str) -> dict:
        from app.core.config import settings

        runtime = ActionRuntime(
            energy=self._energy,
            system=system,
            publisher=OutboxPublisher(self._db) if settings.DEVICE_COMMAND_OUTBOX_ENABLED else self._publisher,
            settings=settings,
            active_commands=await self._active_commands(system.household_id),
        )
        return await self._graph.ainvoke(
            payload,
            context=runtime,
            config={"configurable": {"thread_id": proposal_id}, "recursion_limit": 15},
        )

    async def _claim(self, proposal_id: str) -> bool:
        """Atomically move AWAITING to CONFIRMING. False means another request got there first."""
        result = await self._db.execute(
            update(ActionProposal)
            .where(ActionProposal.id == proposal_id, ActionProposal.status == AWAITING)
            .values(status=CONFIRMING, updated_at=_utcnow())
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    async def _save(
        self,
        response: ActionResponse,
        *,
        user_id: uuid.UUID,
        household_id: str,
        question: str,
        expires_at: datetime | None,
    ) -> None:
        self._db.add(
            ActionProposal(
                id=response.proposal_id,
                user_id=user_id,
                household_id=household_id,
                question=question[:500],
                device=response.device,
                command=response.command,
                duration_minutes=response.duration_minutes,
                status=response.status,
                reasons_json=json.dumps(response.reasons),
                checks_json=json.dumps(response.checks),
                command_id=response.command_id,
                expires_at=expires_at,
            )
        )
        await self._db.flush()

    async def _active_commands(self, household_id: str) -> frozenset[tuple[str, str]]:
        now = _utcnow()
        result = await self._db.execute(
            select(DeviceOverride).where(
                DeviceOverride.household_id == household_id,
                DeviceOverride.status == "active",
            )
        )
        pairs: list[tuple[str, str]] = []
        for row in result.scalars():
            expires = row.expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires is not None and expires <= now:
                continue
            pairs.append((row.device, row.command))
        return frozenset(pairs)

    async def _audit(self, response: ActionResponse, *, user_id: uuid.UUID, household_id: str, event: str) -> None:
        self._db.add(
            ActionAuditEvent(
                proposal_id=response.proposal_id,
                user_id=user_id,
                household_id=household_id,
                event=event,
                reasons_json=json.dumps(response.reasons),
                checks_json=json.dumps(response.checks),
                command_id=response.command_id,
            )
        )
        await self._db.flush()

    async def _audit_row(self, proposal: ActionProposal, *, event: str) -> None:
        self._db.add(
            ActionAuditEvent(
                proposal_id=proposal.id,
                user_id=proposal.user_id,
                household_id=proposal.household_id,
                event=event,
                reasons_json=proposal.reasons_json,
                checks_json=proposal.checks_json,
                command_id=proposal.command_id,
            )
        )

    async def _events(self, proposal_id: str) -> list[ActionAuditEvent]:
        result = await self._db.execute(
            select(ActionAuditEvent)
            .where(ActionAuditEvent.proposal_id == proposal_id)
            .order_by(ActionAuditEvent.created_at.asc())
        )
        return list(result.scalars())

    async def _notify_waiting(self, user_id: uuid.UUID, household_id: str, proposal_id: str, response: ActionResponse) -> None:
        device = (response.device or "device").replace("_", " ")
        await save_notification(
            self._db,
            user_id=user_id,
            household_id=household_id,
            kind="action_waiting_confirmation",
            title="Action waiting for confirmation",
            body=f"Confirm turning {response.command} the {device}.",
            ref_id=proposal_id,
        )

    async def _owned(self, proposal_id: str, user_id: uuid.UUID, household_id: str) -> ActionProposal:
        result = await self._db.execute(select(ActionProposal).where(ActionProposal.id == proposal_id))
        proposal = result.scalar_one_or_none()
        if proposal is None or proposal.user_id != user_id or proposal.household_id != household_id:
            raise ProposalExpiredError("Action not found.")
        return proposal


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; Postgres returns aware ones. Treat naive as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and _aware(expires_at) <= _utcnow()


def _response(proposal_id: str, state: dict, *, expires_at: datetime | None = None) -> ActionResponse:
    interrupted = bool(state.get("__interrupt__"))
    decision = state.get("decision")
    if interrupted or decision == "propose":
        status = "awaiting_confirmation"
    elif decision in {"rejected", "reject"}:
        status = "rejected"
    elif decision == "cancelled":
        status = "cancelled"
    elif decision == "queued":
        status = "queued"
    elif decision == "applied":
        status = "applied"
    elif decision == "failed":
        status = "failed"
    else:
        status = "rejected"
    reasons = list(state.get("reasons") or [])
    device = state.get("device")
    command = state.get("command")
    return ActionResponse(
        proposal_id=proposal_id,
        status=status,  # type: ignore[arg-type]
        device=device,
        command=command,
        reasons=reasons,
        checks=dict(state.get("checks") or {}),
        message=explain(status, reasons, device, command),
        command_id=state.get("command_id"),
        duration_minutes=state.get("duration_minutes"),
        expires_at=expires_at if status == AWAITING else None,
    )


def _audit_event(status: str) -> str:
    return {
        "awaiting_confirmation": "proposed",
        "rejected": "safety_rejected",
        "cancelled": "cancelled",
        "queued": "queued",
        "applied": "applied",
        "failed": "failed",
    }.get(status, status)


def _from_row(proposal: ActionProposal, events: list[ActionAuditEvent]) -> ActionResponse:
    reasons = json.loads(proposal.reasons_json or "[]")
    checks = json.loads(proposal.checks_json or "{}")
    # A proposal nobody answered in time reads as expired even before the sweep records it.
    status = AWAITING if proposal.status == CONFIRMING else proposal.status
    if status == AWAITING and _is_expired(proposal.expires_at):
        status = "expired"
    return ActionResponse(
        proposal_id=proposal.id,
        status=status,  # type: ignore[arg-type]
        device=proposal.device,
        command=proposal.command,
        duration_minutes=proposal.duration_minutes,
        reasons=reasons,
        checks=checks,
        message=explain(status, reasons, proposal.device, proposal.command),
        command_id=proposal.command_id,
        expires_at=proposal.expires_at if status == AWAITING else None,
        audit=[
            {"event": event.event, "command_id": event.command_id, "created_at": event.created_at}
            for event in events
        ],
    )
