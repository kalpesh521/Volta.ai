"""Propose a device action, pause for confirmation, then queue it."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ForbiddenError
from app.modules.assistant.actions.graph import ActionRuntime
from app.modules.assistant.actions.models import ActionProposal
from app.modules.assistant.actions.publisher import CommandPublisher
from app.modules.assistant.actions.safety import explain
from app.modules.assistant.actions.schemas import ActionResponse
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem
from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger("volta.actions")


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
        response = _response(proposal_id, state)
        await self._save(response, user_id=user_id, household_id=system.household_id, question=question)
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
        proposal = await self._owned(proposal_id, user_id, system.household_id)
        if proposal.status != "awaiting_confirmation":
            raise ProposalExpiredError("This action is no longer waiting for confirmation.")
        snapshot = await self._graph.aget_state({"configurable": {"thread_id": proposal_id}})
        if snapshot is None or not snapshot.values:
            raise ProposalExpiredError()
        state = await self._invoke(system, Command(resume={"confirmed": confirmed}), proposal_id)
        response = _response(proposal_id, state)
        proposal.status = response.status
        proposal.reasons_json = json.dumps(response.reasons)
        proposal.checks_json = json.dumps(response.checks)
        proposal.command_id = response.command_id
        proposal.device = response.device
        proposal.command = response.command
        proposal.updated_at = datetime.now(timezone.utc)
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
            publisher=self._publisher,
            settings=settings,
        )
        return await self._graph.ainvoke(
            payload,
            context=runtime,
            config={"configurable": {"thread_id": proposal_id}, "recursion_limit": 15},
        )

    async def _save(self, response: ActionResponse, *, user_id: uuid.UUID, household_id: str, question: str) -> None:
        self._db.add(
            ActionProposal(
                id=response.proposal_id,
                user_id=user_id,
                household_id=household_id,
                question=question[:500],
                device=response.device,
                command=response.command,
                status=response.status,
                reasons_json=json.dumps(response.reasons),
                checks_json=json.dumps(response.checks),
                command_id=response.command_id,
            )
        )
        await self._db.flush()

    async def _owned(self, proposal_id: str, user_id: uuid.UUID, household_id: str) -> ActionProposal:
        result = await self._db.execute(select(ActionProposal).where(ActionProposal.id == proposal_id))
        proposal = result.scalar_one_or_none()
        if proposal is None or proposal.user_id != user_id or proposal.household_id != household_id:
            raise ProposalExpiredError("Action not found.")
        return proposal


def _response(proposal_id: str, state: dict) -> ActionResponse:
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
    )
