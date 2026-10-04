"""
Assistant HTTP endpoints (read-only phase).

  GET  /assistant/status                    model / mode status (no secrets)
  POST /assistant/me/ask                    caller's primary home (?household_id= optional)
  POST /assistant/{household_id}/ask        a specific home the caller owns

Ownership and completed onboarding are enforced by the energy access
dependencies before the graph runs.

Do not add `from __future__ import annotations` in this file. slowapi wraps
the handlers and cannot resolve postponed annotations.
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from app.ai.config import AISettings, ai_settings
from app.ai.llm.factory import ChatModelFactory
from app.ai.llm.gateway import StructuredLLM
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.assistant.deps import (
    get_assistant_service,
    get_assistant_settings,
    get_chat_model_factory,
    get_structured_llm,
)
from app.modules.assistant.schemas import AskRequest, AssistantResponse, AssistantStatusOut
from app.modules.assistant.service import AssistantService, assistant_status
from app.modules.energy.access import require_my_completed_system, require_owned_household
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/assistant", tags=["assistant"])

_MAX_REQUEST_ID = 64


def _request_id(request: Request) -> str:
    supplied = (request.headers.get("x-request-id") or "").strip()
    if supplied and supplied.isprintable():
        return supplied[:_MAX_REQUEST_ID]
    return uuid.uuid4().hex


@router.get(
    "/status",
    response_model=AssistantStatusOut,
    summary="Configured models and whether the assistant runs in LLM or deterministic mode",
)
async def get_status(
    _: User = Depends(get_current_user),
    settings: AISettings = Depends(get_assistant_settings),
    llm: StructuredLLM = Depends(get_structured_llm),
    factory: ChatModelFactory = Depends(get_chat_model_factory),
) -> AssistantStatusOut:
    return assistant_status(settings, llm, factory)


@router.post(
    "/me/ask",
    response_model=AssistantResponse,
    summary="Ask Suryaa about the authenticated user's home (read-only)",
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def ask_my_assistant(
    request: Request,
    payload: Annotated[AskRequest, Body()],
    system: SolarSystem = Depends(require_my_completed_system),
    service: AssistantService = Depends(get_assistant_service),
) -> AssistantResponse:
    return await service.ask(system, payload.question, request_id=_request_id(request))


@router.post(
    "/{household_id}/ask",
    response_model=AssistantResponse,
    summary="Ask Suryaa about a specific home the caller owns (read-only)",
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def ask_household_assistant(
    request: Request,
    payload: Annotated[AskRequest, Body()],
    system: SolarSystem = Depends(require_owned_household),
    service: AssistantService = Depends(get_assistant_service),
) -> AssistantResponse:
    return await service.ask(system, payload.question, request_id=_request_id(request))
