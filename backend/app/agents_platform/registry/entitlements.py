"""
Plan-tier rules for agents (tracker 3.13).

Which agents need which plan is configuration, not code, because the rules are a product decision that will change
(tracker Y7): `AI_PLUS_ONLY_AGENTS` names the agents, `AI_PLUS_ONLY_WHAT_IF` guards the bill what-if simulator. The
names are checked against the closed agent set at startup, so a typo stops the process instead of quietly leaving an
expensive agent open to everyone.

A tier the system does not recognise is `free` (see `Tier.parse`), so a bad value in the database can never grant a
paid feature. The check runs inside the supervisor's agent node, so every route (ask, stream, a follow-up that
inherits a planning topic) goes through it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents_platform.budgets.tiers import Tier
from app.agents_platform.contracts import AgentName
from app.ai.config import AISettings


def parse_plus_only(csv: str) -> frozenset[AgentName]:
    names = [part.strip() for part in csv.split(",") if part.strip()]
    unknown = [n for n in names if n not in {a.value for a in AgentName}]
    if unknown:
        raise ValueError(
            f"AI_PLUS_ONLY_AGENTS names unknown agents {unknown}; known: {', '.join(a.value for a in AgentName)}"
        )
    return frozenset(AgentName(n) for n in names)


@dataclass(frozen=True)
class EntitlementRules:
    """What the deployment restricts. Built once at startup."""

    plus_only: frozenset[AgentName] = frozenset()
    what_if_plus_only: bool = False

    @classmethod
    def from_settings(cls, settings: AISettings) -> EntitlementRules:
        return cls(parse_plus_only(settings.AI_PLUS_ONLY_AGENTS), settings.AI_PLUS_ONLY_WHAT_IF)

    def for_tier(self, tier: Tier) -> Entitlements:
        return Entitlements(tier=tier, rules=self)


@dataclass(frozen=True)
class Entitlements:
    """What one user may use. The default allows everything (tests, internal callers)."""

    tier: Tier = Tier.PLUS
    rules: EntitlementRules = EntitlementRules()

    def allows(self, agent: AgentName) -> bool:
        return self.tier is Tier.PLUS or agent not in self.rules.plus_only

    @property
    def what_if_allowed(self) -> bool:
        return self.tier is Tier.PLUS or not self.rules.what_if_plus_only
