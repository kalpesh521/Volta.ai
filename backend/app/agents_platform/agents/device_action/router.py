"""
  POST /assistant/actions/propose
  GET  /assistant/actions
  GET  /assistant/actions/{proposal_id}
  POST /assistant/actions/{proposal_id}/confirm
  POST /assistant/actions/{proposal_id}/reject
  POST /assistant/actions/{proposal_id}/cancel

Do not add `from __future__ import annotations` in this file.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.agents_platform.api.deps import DbScope, get_db_scope
from app.ai.config import ai_settings
from app.core.config import settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.agents_platform.agents.device_action.deps import get_action_service, get_outbox_relay
from app.agents_platform.agents.device_action.outbox import OutboxRelay, dispatch_now
from app.agents_platform.agents.device_action.schemas import ActionResponse, ConfirmActionRequest, ProposeActionRequest
from app.agents_platform.agents.device_action.service import ActionService
from app.modules.energy.access import require_my_completed_system
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/assistant/actions", tags=["assistant"])


@router.post("/propose", response_model=ActionResponse)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def propose_action(
    request: Request,
    payload: ProposeActionRequest,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return await service.propose(system, current_user.id, payload.question)


@router.get("", response_model=list[ActionResponse], summary="List recent device actions")
async def list_actions(
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> list[ActionResponse]:
    return await service.list_for_home(system, current_user.id)


@router.get("/{proposal_id}", response_model=ActionResponse, summary="Get a device action")
async def get_action(
    proposal_id: str,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return await service.get(system, current_user.id, proposal_id)


@router.post("/{proposal_id}/confirm", response_model=ActionResponse)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def confirm_action(
    request: Request,
    proposal_id: str,
    payload: ConfirmActionRequest,
    background: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
    relay: OutboxRelay = Depends(get_outbox_relay),
    scope: DbScope = Depends(get_db_scope),
) -> ActionResponse:
    response = await service.confirm(system, current_user.id, proposal_id, payload.confirmed)
    if settings.DEVICE_COMMAND_OUTBOX_ENABLED and response.status == "queued" and response.command_id:
        # The command is already a durable row. Deliver it as soon as the response is out; the relay process is the
        # safety net if this does not run.
        background.add_task(dispatch_now, relay, scope, response.command_id)
    return response


@router.post("/{proposal_id}/reject", response_model=ActionResponse, summary="Reject a proposed device action")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def reject_action(
    request: Request,
    proposal_id: str,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return await service.reject(system, current_user.id, proposal_id)


@router.post("/{proposal_id}/cancel", response_model=ActionResponse, summary="Cancel a device action")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def cancel_action(
    request: Request,
    proposal_id: str,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return await service.cancel(system, current_user.id, proposal_id)
