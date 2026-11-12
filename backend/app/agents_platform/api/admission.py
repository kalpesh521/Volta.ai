"""
Admission: the checks that decide, before any work starts, whether a request runs and how.

    cool-down   a user whose inputs keep being refused as injection attempts waits           → 429 + Retry-After
    kill switch `ai.llm.enabled` off                                                       → runs without the model
    budget      daily tokens used up: `degrade` (runs without the model) or `reject`      → 429 until the next UTC day

Runs as a FastAPI dependency, so all four endpoints (and both the plain and the streamed form) share one
implementation and a refusal is a plain HTTP error before the first byte is streamed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents_platform.budgets.service import BudgetStatus
from app.agents_platform.budgets.tiers import Tier
from app.agents_platform.platform import Platform
from app.agents_platform.registry.flags import LLM_FLAG, FlagSnapshot
from app.core.exceptions import AppError
from app.core.metrics import BUDGET_EVENTS, KILL_SWITCH_HITS
from app.models.user import User

logger = logging.getLogger("volta.agents.admission")

DEGRADED_BUDGET = "budget_exhausted"
DEGRADED_LLM_OFF = "llm_disabled"


class RetryLaterError(AppError):
    status_code = 429

    def __init__(self, message: str, *, code: str, retry_after: int) -> None:
        super().__init__(message, code=code, details={"retry_after_seconds": retry_after})
        self.headers = {"Retry-After": str(retry_after)}


@dataclass(frozen=True)
class Admission:
    tier: Tier
    flags: FlagSnapshot
    budget: BudgetStatus | None = None
    degraded_reason: str | None = None

    @property
    def llm_allowed(self) -> bool:
        return self.degraded_reason is None


async def admit(user: User, platform: Platform) -> Admission:
    settings = platform.settings
    wait = await platform.abuse.remaining(user.id)
    if wait:
        raise RetryLaterError(
            "Too many of your messages were refused. Please wait before asking again.",
            code="assistant_cooldown",
            retry_after=wait,
        )

    tier = Tier.parse(user.plan_tier)
    flags = await platform.flags.snapshot()
    degraded: str | None = None
    if not flags.llm_enabled:
        KILL_SWITCH_HITS.labels(flag=LLM_FLAG).inc()
        degraded = DEGRADED_LLM_OFF

    status: BudgetStatus | None = None
    if settings.AI_BUDGET_ENABLED:
        status = await platform.budget.status(user.id, tier)
        if not status.known:
            BUDGET_EVENTS.labels(tier=tier.value, outcome="store_error").inc()
        elif status.exhausted:
            if settings.AI_BUDGET_EXHAUSTED_ACTION == "reject":
                BUDGET_EVENTS.labels(tier=tier.value, outcome="rejected").inc()
                raise RetryLaterError(
                    "You have used today's AI allowance. It resets at midnight UTC.",
                    code="token_budget_exceeded",
                    retry_after=status.seconds_until_reset,
                )
            BUDGET_EVENTS.labels(tier=tier.value, outcome="degraded").inc()
            degraded = degraded or DEGRADED_BUDGET
        else:
            BUDGET_EVENTS.labels(tier=tier.value, outcome="ok").inc()
    return Admission(tier=tier, flags=flags, budget=status, degraded_reason=degraded)


def degraded_warning(reason: str) -> str:
    if reason == DEGRADED_BUDGET:
        return "You have used today's AI allowance, so this is a verified summary written without the AI model."
    return "The AI model is temporarily switched off, so this is a verified summary."
