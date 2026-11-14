"""
The Phase 2 services of one deployment, built once at startup from settings.

Building them eagerly is the point: a malformed flag name, an unknown prompt version or a missing vault key stops
the process at boot with a clear message, instead of failing the first request that happens to touch it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents_platform.budgets.service import TokenBudget
from app.agents_platform.budgets.store import BudgetStore, InMemoryBudgetStore, RedisBudgetStore
from app.agents_platform.guardrails.abuse import AbuseGuard, AbuseStore, InMemoryAbuseStore, RedisAbuseStore
from app.agents_platform.guardrails.input_guard import InputGuard
from app.agents_platform.guardrails.pii.vault import InMemoryVault, RedisVault, Vault, parse_key
from app.agents_platform.infra.redis import RedisProvider, get_redis_provider
from app.agents_platform.locks import ConversationLock, InMemoryConversationLock, RedisConversationLock
from app.agents_platform.registry.entitlements import EntitlementRules
from app.agents_platform.registry.flags import (
    FeatureFlags,
    FlagStore,
    InMemoryFlagStore,
    RedisFlagStore,
    parse_disabled,
)
from app.agents_platform.registry.prompts import ResolvedPrompts, resolve_prompts
from app.ai.config import AISettings


@dataclass(frozen=True)
class Platform:
    settings: AISettings
    guard: InputGuard
    vault: Vault
    abuse: AbuseGuard
    budget: TokenBudget
    flags: FeatureFlags
    prompts: ResolvedPrompts
    locks: ConversationLock
    entitlements: EntitlementRules


def _vault(settings: AISettings, provider: RedisProvider) -> Vault:
    if settings.AI_PII_VAULT_BACKEND == "memory":
        return InMemoryVault(ttl_seconds=settings.AI_PII_VAULT_TTL_SECONDS)
    if settings.AI_PII_VAULT_KEY is None:
        raise ValueError(
            "AI_PII_VAULT_KEY is required when AI_PII_VAULT_BACKEND=redis (urlsafe base64 of 32 bytes). Generate one: "
            "python -c \"from app.agents_platform.guardrails.pii.vault import generate_key; print(generate_key())\""
        )
    return RedisVault(
        provider, key=parse_key(settings.AI_PII_VAULT_KEY.get_secret_value()), ttl_seconds=settings.AI_PII_VAULT_TTL_SECONDS
    )


def build_platform(settings: AISettings, provider: RedisProvider | None = None) -> Platform:
    shared = settings.AI_PLATFORM_STORE == "redis"
    provider = provider or get_redis_provider()

    budget_store: BudgetStore = RedisBudgetStore(provider) if shared else InMemoryBudgetStore()
    abuse_store: AbuseStore = RedisAbuseStore(provider) if shared else InMemoryAbuseStore()
    flag_store: FlagStore = RedisFlagStore(provider) if shared else InMemoryFlagStore()

    return Platform(
        settings=settings,
        guard=InputGuard(settings),
        vault=_vault(settings, provider),
        abuse=AbuseGuard(
            abuse_store,
            strike_limit=settings.AI_GUARD_STRIKE_LIMIT,
            window_seconds=settings.AI_GUARD_STRIKE_WINDOW_SECONDS,
            base_cooldown_seconds=settings.AI_GUARD_COOLDOWN_BASE_SECONDS,
            max_cooldown_seconds=settings.AI_GUARD_COOLDOWN_MAX_SECONDS,
        ),
        budget=TokenBudget(budget_store, settings),
        flags=FeatureFlags(flag_store, baseline_disabled=parse_disabled(settings.AI_FLAGS_DISABLED)),
        prompts=resolve_prompts(settings),
        locks=RedisConversationLock(provider) if shared else InMemoryConversationLock(),
        entitlements=EntitlementRules.from_settings(settings),  # a typo in the agent list stops startup
    )
