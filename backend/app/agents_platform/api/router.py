"""
v2 assistant endpoints: a supervisor over specialist agents, with conversation memory.

  POST /v2/assistant/me/ask                  the caller's primary home (?household_id= optional)
  POST /v2/assistant/{household_id}/ask      a specific home the caller owns
  POST /v2/assistant/me/ask/stream           same, as server-sent events (status..., final)
  POST /v2/assistant/{household_id}/ask/stream

The v1 endpoints (/assistant/...) stay live. The response is a superset of v1's, so a client can switch by
changing the path. A device action comes back as a `proposal`; confirm it with the existing
/assistant/actions/{id}/confirm.

Do not add `from __future__ import annotations` in this file. slowapi wraps the handlers and cannot
resolve postponed annotations.
"""
from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse

from app.agents_platform.api.deps import (
    DbScope,
    SupervisorServiceFactory,
    get_db_scope,
    get_service_factory,
    get_supervisor_service,
)
from app.agents_platform.api.schemas import AskV2Request, SupervisorResponse
from app.agents_platform.service import SupervisorService
from app.ai.config import ai_settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.core.request_context import get_request_id, new_request_id
from app.models.user import User
from app.modules.energy.access import require_my_completed_system, require_owned_household
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/v2/assistant", tags=["assistant-v2"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def _request_id() -> str:
    return get_request_id() or new_request_id()


async def _stream(
    *,
    payload: AskV2Request,
    user: User,
    system: SolarSystem,
    factory: SupervisorServiceFactory,
    scope: DbScope,
) -> StreamingResponse:
    """
    Open the stream's database session and conversation first, so a bad conversation id is a plain 404.
    The session stays open for the whole stream and commits when the stream ends.
    """
    stack = AsyncExitStack()
    try:
        db = await stack.enter_async_context(scope())
        events = await factory(db).stream(
            system,
            user.id,
            payload.question,
            conversation_id=payload.conversation_id,
            request_id=_request_id(),
        )
    except BaseException:
        await stack.aclose()
        raise

    async def body():
        try:
            async for chunk in events:
                yield chunk
        finally:
            await stack.aclose()

    return StreamingResponse(body(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "/me/ask",
    response_model=SupervisorResponse,
    summary="Ask Suryaa about the authenticated user's home",
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def ask_my_assistant(
    request: Request,
    payload: Annotated[AskV2Request, Body()],
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: SupervisorService = Depends(get_supervisor_service),
) -> SupervisorResponse:
    return await service.ask(
        system, user.id, payload.question, conversation_id=payload.conversation_id, request_id=_request_id()
    )


@router.post(
    "/me/ask/stream",
    summary="Ask Suryaa about the authenticated user's home, streamed as server-sent events",
    response_class=StreamingResponse,
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def stream_my_assistant(
    request: Request,
    payload: Annotated[AskV2Request, Body()],
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    factory: SupervisorServiceFactory = Depends(get_service_factory),
    scope: DbScope = Depends(get_db_scope),
) -> StreamingResponse:
    return await _stream(payload=payload, user=user, system=system, factory=factory, scope=scope)


@router.post(
    "/{household_id}/ask",
    response_model=SupervisorResponse,
    summary="Ask Suryaa about a specific home the caller owns",
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def ask_household_assistant(
    request: Request,
    payload: Annotated[AskV2Request, Body()],
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_owned_household),
    service: SupervisorService = Depends(get_supervisor_service),
) -> SupervisorResponse:
    return await service.ask(
        system, user.id, payload.question, conversation_id=payload.conversation_id, request_id=_request_id()
    )


@router.post(
    "/{household_id}/ask/stream",
    summary="Ask about a specific home the caller owns, streamed as server-sent events",
    response_class=StreamingResponse,
)
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def stream_household_assistant(
    request: Request,
    payload: Annotated[AskV2Request, Body()],
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_owned_household),
    factory: SupervisorServiceFactory = Depends(get_service_factory),
    scope: DbScope = Depends(get_db_scope),
) -> StreamingResponse:
    return await _stream(payload=payload, user=user, system=system, factory=factory, scope=scope)
