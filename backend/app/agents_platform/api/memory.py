"""
"What Suryaa remembers": the user's view of long-term memory.

  GET    /v2/assistant/memory            list the items of a home
  POST   /v2/assistant/memory            save one (also what "remember that ..." does in the chat)
  GET    /v2/assistant/memory/export     everything of this home as JSON
  PATCH  /v2/assistant/memory/{id}       edit text, kind or expiry
  DELETE /v2/assistant/memory/{id}       forget one (hard delete)
  DELETE /v2/assistant/memory            forget everything of this home (hard delete)

The home is the caller's primary home, or `?household_id=` for another home they own (the same resolution as
`/v2/assistant/me/ask`). Memory is separate per home. An id that is unknown, another user's, or another home's
is a 404, never a 403.

Do not add `from __future__ import annotations` in this file. slowapi wraps the handlers and cannot
resolve postponed annotations.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.memory_store.models import MemorySource
from app.agents_platform.memory_store.schemas import (
    MemoryCreateIn,
    MemoryCreateOut,
    MemoryDeleteAllOut,
    MemoryExportOut,
    MemoryItemOut,
    MemoryListOut,
    MemoryUpdateIn,
)
from app.agents_platform.memory_store.service import MemoryService
from app.ai.config import ai_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.energy.access import require_my_completed_system
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/v2/assistant/memory", tags=["assistant-memory"])

ItemId = Path(min_length=1, max_length=64, description="Memory item id")


def get_memory_service(db: AsyncSession = Depends(get_db)) -> MemoryService:
    return MemoryService(db)


@router.get("", response_model=MemoryListOut, summary="What Suryaa remembers about a home")
async def list_memory(
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryListOut:
    items = await service.list_items(user.id, system.household_id)
    return MemoryListOut(
        household_id=system.household_id,
        items=[MemoryItemOut.model_validate(i) for i in items],
        count=len(items),
        max_items=service.max_items,
        item_max_chars=service.item_max_chars,
    )


@router.post("", response_model=MemoryCreateOut, summary="Ask Suryaa to remember a fact about this home")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def create_memory(
    request: Request,
    response: Response,
    payload: MemoryCreateIn,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryCreateOut:
    saved = await service.create(
        user.id,
        system.household_id,
        payload.text,
        kind=payload.kind,
        expires_at=payload.expires_at,
        source=MemorySource.API,
    )
    # 201 for a new item, 200 when Suryaa already remembered the same thing.
    response.status_code = status.HTTP_201_CREATED if saved.created else status.HTTP_200_OK
    return MemoryCreateOut(item=MemoryItemOut.model_validate(saved.item), created=saved.created, masked=list(saved.masked))


@router.get("/export", response_model=MemoryExportOut, summary="Download everything Suryaa remembers about this home")
async def export_memory(
    response: Response,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryExportOut:
    items = await service.export(user.id, system.household_id)
    response.headers["Content-Disposition"] = f'attachment; filename="suryaa-memory-{system.household_id}.json"'
    response.headers["Cache-Control"] = "no-store"
    return MemoryExportOut(
        exported_at=datetime.now(timezone.utc),
        household_id=system.household_id,
        count=len(items),
        items=[MemoryItemOut.model_validate(i) for i in items],
    )


@router.patch("/{item_id}", response_model=MemoryItemOut, summary="Edit one remembered item")
@limiter.limit(ai_settings.RATE_LIMIT_ASSISTANT)
async def update_memory(
    request: Request,
    payload: MemoryUpdateIn,
    item_id: str = ItemId,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryItemOut:
    fields = payload.model_fields_set
    saved = await service.update(
        user.id,
        system.household_id,
        item_id,
        text=payload.text,
        kind=payload.kind,
        expires_at=payload.expires_at,
        clear_expiry="expires_at" in fields and payload.expires_at is None,
    )
    return MemoryItemOut.model_validate(saved.item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Forget one item")
async def delete_memory(
    item_id: str = ItemId,
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> Response:
    await service.delete(user.id, system.household_id, item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", response_model=MemoryDeleteAllOut, summary="Forget everything Suryaa remembers about this home")
async def delete_all_memory(
    user: User = Depends(get_current_user),
    system: SolarSystem = Depends(require_my_completed_system),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryDeleteAllOut:
    return MemoryDeleteAllOut(deleted=await service.delete_all(user.id, system.household_id))
