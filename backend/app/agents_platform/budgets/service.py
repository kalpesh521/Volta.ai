"""
Daily token budget per user (gate G3).

Flow, one request:

    admission (FastAPI dependency)   read the counter: `status()`
        under the limit           → normal request
        at or over the limit      → `degrade` (answer from facts, no model) or `reject` (429)
    service                          after the answer: `debit()` the tokens the request really used

The check happens before the work and the debit after it, so a burst of concurrent requests can overshoot the
limit by what those few requests spend. That bounded overshoot is the price of never blocking a request on a
counter write, and the rate limit already bounds the burst.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.agents_platform.budgets.store import BudgetStore
from app.agents_platform.budgets.tiers import Tier, daily_token_limit
from app.ai.config import AISettings
from app.core.metrics import BUDGET_EVENTS, BUDGET_TOKENS

logger = logging.getLogger("volta.agents.budget")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BudgetStatus:
    tier: Tier
    limit: int
    used: int
    seconds_until_reset: int
    known: bool = True  # False when the store could not be read (fail-open)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.known and self.used >= self.limit


class TokenBudget:
    def __init__(
        self, store: BudgetStore, settings: AISettings, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock

    def _day_key(self, user_id: uuid.UUID | str, now: datetime) -> str:
        return f"{user_id}:{now:%Y%m%d}"

    async def status(self, user_id: uuid.UUID | str, tier: Tier) -> BudgetStatus:
        now = self._clock()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        used = await self._store.used(self._day_key(user_id, now))
        return BudgetStatus(
            tier=tier,
            limit=daily_token_limit(tier, self._settings),
            used=used or 0,
            seconds_until_reset=max(1, int((tomorrow - now).total_seconds())),
            known=used is not None,
        )

    async def debit(self, user_id: uuid.UUID | str, tier: Tier, tokens: int) -> None:
        if tokens <= 0:
            return
        recorded = await self._store.add(self._day_key(user_id, self._clock()), tokens)
        if recorded:
            BUDGET_TOKENS.labels(tier=tier.value).inc(tokens)
        else:
            BUDGET_EVENTS.labels(tier=tier.value, outcome="store_error").inc()
