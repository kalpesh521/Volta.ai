"""
Automation rules over HTTP (tracker 4.1, 4.2).

  GET    /v2/assistant/automations                    the caller's rules for the home
  POST   /v2/assistant/automations                    propose a rule in the DSL (validated, dry-run, stored pending)
  GET    /v2/assistant/automations/{id}
  POST   /v2/assistant/automations/{id}/approve       body: the fingerprint that was shown
  POST   /v2/assistant/automations/{id}/reject
  POST   /v2/assistant/automations/{id}/pause
  POST   /v2/assistant/automations/{id}/resume
  DELETE /v2/assistant/automations/{id}

A rule written by the assistant arrives here as `pending_approval` too: there is no other way to turn one on.
"""
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.agents.planning_data import GatewayPlanningData
from app.agents_platform.automation.context import DRY_RUN_RECORDS, device_names, readings_from, validation_context
from app.agents_platform.automation.schemas import ApproveAutomationRequest, AutomationOut, ProposeAutomationRequest
from app.agents_platform.automation.service import AutomationService
from app.ai.config import ai_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.energy.access import require_my_completed_system
from app.modules.energy.deps import get_energy_service
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/v2/assistant/automations", tags=["automations"])


def get_automation_service(db: AsyncSession = Depends(get_db)) -> AutomationService:
    return AutomationService(db, max_rules=ai_settings.AI_AUTOMATION_MAX_RULES, max_pending=ai_settings.AI_AUTOMATION_MAX_PENDING)


@router.get("", response_model=list[AutomationOut], summary="List your automations for this home")
async def list_automations(
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
    energy: EnergyService = Depends(get_energy_service),
) -> list[AutomationOut]:
    profile = EnergyDataGateway(energy, system).profile()
    return await service.list(user_id=user.id, household_id=system.household_id, device_names=device_names(profile))


@router.post("", response_model=AutomationOut, status_code=status.HTTP_201_CREATED, summary="Propose an automation rule")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def propose_automation(
    request: Request,
    payload: ProposeAutomationRequest,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
    energy: EnergyService = Depends(get_energy_service),
) -> AutomationOut:
    data = GatewayPlanningData(EnergyDataGateway(energy, system))
    profile = data.profile()
    live = await data.live()
    records = await data.history(DRY_RUN_RECORDS) if live is not None else []
    timezone = live.location.timezone if live is not None and live.location is not None else None
    return await service.propose(
        user_id=user.id,
        household_id=system.household_id,
        raw=payload.rule,
        context=validation_context(profile, timezone=timezone),
        readings=readings_from(records),
        device_names=device_names(profile),
        source="api",
    )


@router.get("/{automation_id}", response_model=AutomationOut)
async def get_automation(
    automation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
    energy: EnergyService = Depends(get_energy_service),
) -> AutomationOut:
    profile = EnergyDataGateway(energy, system).profile()
    return await service.get(user_id=user.id, household_id=system.household_id, automation_id=automation_id, device_names=device_names(profile))


@router.post("/{automation_id}/approve", response_model=AutomationOut, summary="Approve exactly the rule you were shown")
async def approve_automation(
    automation_id: uuid.UUID,
    payload: ApproveAutomationRequest,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
) -> AutomationOut:
    return await service.approve(user_id=user.id, household_id=system.household_id, automation_id=automation_id, fingerprint=payload.fingerprint)


@router.post("/{automation_id}/reject", response_model=AutomationOut)
async def reject_automation(
    automation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
) -> AutomationOut:
    return await service.reject(user_id=user.id, household_id=system.household_id, automation_id=automation_id)


@router.post("/{automation_id}/pause", response_model=AutomationOut)
async def pause_automation(
    automation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
) -> AutomationOut:
    return await service.pause(user_id=user.id, household_id=system.household_id, automation_id=automation_id)


@router.post("/{automation_id}/resume", response_model=AutomationOut)
async def resume_automation(
    automation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
) -> AutomationOut:
    return await service.resume(user_id=user.id, household_id=system.household_id, automation_id=automation_id)


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(
    automation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: AutomationService = Depends(get_automation_service),
) -> Response:
    await service.delete(user_id=user.id, household_id=system.household_id, automation_id=automation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
