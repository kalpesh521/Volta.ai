"""
  POST /assistant/actions/propose
  POST /assistant/actions/{proposal_id}/confirm

Do not add `from __future__ import annotations` in this file.
"""
from fastapi import APIRouter, Depends, Request

from app.ai.config import ai_settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.assistant.actions.deps import get_action_service
from app.modules.assistant.actions.schemas import ActionResponse, ConfirmActionRequest, ProposeActionRequest
from app.modules.assistant.actions.service import ActionService
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


@router.post("/{proposal_id}/confirm", response_model=ActionResponse)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def confirm_action(
    request: Request,
    proposal_id: str,
    payload: ConfirmActionRequest,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: ActionService = Depends(get_action_service),
) -> ActionResponse:
    return await service.confirm(system, current_user.id, proposal_id, payload.confirmed)
