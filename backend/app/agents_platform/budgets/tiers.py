"""Plan tiers. The tier is stored on the user (`users.plan_tier`); the limits live in settings."""
from __future__ import annotations

from enum import StrEnum

from app.ai.config import AISettings


class Tier(StrEnum):
    FREE = "free"
    PLUS = "plus"

    @classmethod
    def parse(cls, value: str | None) -> Tier:
        """An unknown or missing tier is the most restricted one, never an error and never a free upgrade."""
        try:
            return cls(value) if value else cls.FREE
        except ValueError:
            return cls.FREE


def daily_token_limit(tier: Tier, settings: AISettings) -> int:
    return {
        Tier.FREE: settings.AI_BUDGET_DAILY_TOKENS_FREE,
        Tier.PLUS: settings.AI_BUDGET_DAILY_TOKENS_PLUS,
    }[tier]
