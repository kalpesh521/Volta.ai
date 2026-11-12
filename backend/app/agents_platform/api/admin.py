"""
Operator endpoints for the agent platform. Suryaa admins only.

  GET    /v2/assistant/admin/platform        what is on: flags, prompt versions, models, guard and budget settings
  PUT    /v2/assistant/admin/flags/{flag}    switch a flag on or off for every API pod (a kill switch), no deploy
  DELETE /v2/assistant/admin/flags/{flag}    remove the override and fall back to the environment baseline

Flag names are a closed set (see `registry/flags.py`); an unknown name is a 400, so a typo cannot silently fail
to switch anything off. Every change is logged with the admin's id.
"""
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from app.agents_platform.api.deps import get_platform
from app.agents_platform.platform import Platform
from app.agents_platform.registry.models import describe_models
from app.ai.llm.gateway import StructuredLLM
from app.core.deps import get_current_user
from app.core.exceptions import AppError
from app.models.user import User
from app.modules.assistant.deps import get_structured_llm
from app.modules.knowledge.access import require_suryaa_admin

logger = logging.getLogger("volta.agents.admin")

router = APIRouter(prefix="/v2/assistant/admin", tags=["assistant-v2-admin"])


class FlagChange(BaseModel):
    enabled: bool


class ModelRow(BaseModel):
    role: str
    provider: str
    model: str
    available: bool


class PromptRow(BaseModel):
    active: str
    registered: list[str]


class PlatformStatus(BaseModel):
    flags: dict[str, bool]
    prompts: dict[str, PromptRow]
    models: list[ModelRow]
    guard: dict[str, object]
    budget: dict[str, object]
    stores: dict[str, str]


def _admin(user: User = Depends(get_current_user)) -> User:
    return require_suryaa_admin(user)


@router.get("/platform", response_model=PlatformStatus, summary="Flags, prompt versions, models and guardrail settings")
async def platform_status(
    _: User = Depends(_admin),
    platform: Platform = Depends(get_platform),
    llm: StructuredLLM = Depends(get_structured_llm),
) -> PlatformStatus:
    s = platform.settings
    snapshot = await platform.flags.snapshot()
    return PlatformStatus(
        flags=snapshot.as_dict(),
        prompts={name: PromptRow(active=row["active"], registered=list(row["registered"]))  # type: ignore[arg-type]
                 for name, row in platform.prompts.as_dict().items()},
        models=[ModelRow(**m.__dict__) for m in describe_models(s, llm)],
        guard={
            "mode": s.AI_GUARD_MODE,
            "pii_mode": s.AI_PII_MODE,
            "output_policy_mode": s.AI_OUTPUT_POLICY_MODE,
            "classifier_enabled": s.AI_GUARD_CLASSIFIER_ENABLED,
            "block_score": s.AI_GUARD_BLOCK_SCORE,
            "grey_score": s.AI_GUARD_GREY_SCORE,
        },
        budget={
            "enabled": s.AI_BUDGET_ENABLED,
            "exhausted_action": s.AI_BUDGET_EXHAUSTED_ACTION,
            "daily_tokens": {"free": s.AI_BUDGET_DAILY_TOKENS_FREE, "plus": s.AI_BUDGET_DAILY_TOKENS_PLUS},
        },
        stores={"platform": s.AI_PLATFORM_STORE, "pii_vault": s.AI_PII_VAULT_BACKEND},
    )


async def _change(flag: str, apply, admin: User, platform: Platform, action: str) -> dict[str, object]:
    try:
        stored = await apply(flag)
    except ValueError as exc:
        raise AppError(str(exc), code="unknown_flag") from exc
    if not stored:
        raise AppError("The flag store is unavailable, so nothing was changed.", code="flag_store_unavailable", status_code=503)
    logger.warning("feature flag %s by admin=%s flag=%s", action, admin.id, flag)
    return {"flags": (await platform.flags.snapshot()).as_dict()}


@router.put("/flags/{flag}", summary="Switch a flag on or off (kill switch)")
async def set_flag(
    flag: str,
    change: Annotated[FlagChange, Body()],
    admin: User = Depends(_admin),
    platform: Platform = Depends(get_platform),
) -> dict[str, object]:
    return await _change(flag, lambda f: platform.flags.set(f, change.enabled), admin, platform,
                         f"set enabled={change.enabled}")


@router.delete("/flags/{flag}", summary="Remove a flag override")
async def clear_flag(
    flag: str, admin: User = Depends(_admin), platform: Platform = Depends(get_platform)
) -> dict[str, object]:
    return await _change(flag, platform.flags.clear, admin, platform, "override cleared")
