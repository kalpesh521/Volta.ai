"""
  GET  /assistant/me/recommendations
  GET  /assistant/notifications
  POST /assistant/notifications/scan
  POST /assistant/notifications/{notification_id}/read
  GET  /assistant/automations
  POST /assistant/automations
  POST /assistant/automations/evaluate

Do not add `from __future__ import annotations` in this file.
"""
import uuid

from fastapi import APIRouter, Depends, Request

from app.ai.config import ai_settings
from app.core.deps import get_current_user
from app.core.exceptions import AppError
from app.core.rate_limit import limiter
from app.models.user import User
from app.agents_platform.agents.device_action.deps import get_action_service
from app.agents_platform.agents.device_action.service import ActionService
from app.modules.assistant.ops.schemas import (
    AutomationIn,
    AutomationOut,
    AutomationResult,
    NotificationOut,
    RecommendationOut,
)
from app.modules.assistant.ops.service import HomeOpsService
from app.modules.energy.access import require_my_completed_system
from app.modules.energy.deps import get_energy_service
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

class NotificationNotFoundError(AppError):
    status_code = 404
    code = "notification_not_found"

    def __init__(self) -> None:
        super().__init__("Notification not found.")


router = APIRouter(prefix="/assistant", tags=["assistant"])


def get_home_ops(
    db: AsyncSession = Depends(get_db),
    energy: EnergyService = Depends(get_energy_service),
    actions: ActionService = Depends(get_action_service),
) -> HomeOpsService:
    return HomeOpsService(db, energy, actions)


@router.get("/me/recommendations", response_model=RecommendationOut, summary="Calculated home recommendations")
async def recommendations(
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> RecommendationOut:
    return await service.recommendations(system)


@router.get("/notifications", response_model=list[NotificationOut], summary="List in-app alerts")
async def list_notifications(
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> list[NotificationOut]:
    return await service.list_notifications(system, current_user.id)


@router.post("/notifications/scan", response_model=list[NotificationOut], summary="Create alerts from live energy")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def scan_notifications(
    request: Request,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> list[NotificationOut]:
    return await service.scan_alerts(system, current_user.id)


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut, summary="Mark an alert as read")
async def read_notification(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> NotificationOut:
    row = await service.mark_read(system, current_user.id, notification_id)
    if row is None:
        raise NotificationNotFoundError()
    return row


@router.get("/automations", response_model=list[AutomationOut], summary="List automation rules")
async def list_automations(
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> list[AutomationOut]:
    return await service.list_rules(system, current_user.id)


@router.post("/automations", response_model=AutomationOut, status_code=201, summary="Create an automation rule")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def create_automation(
    request: Request,
    payload: AutomationIn,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> AutomationOut:
    return await service.create_rule(system, current_user.id, payload)


@router.post("/automations/evaluate", response_model=list[AutomationResult], summary="Run automation rules once")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def evaluate_automations(
    request: Request,
    current_user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: HomeOpsService = Depends(get_home_ops),
) -> list[AutomationResult]:
    return await service.evaluate(system, current_user.id)
