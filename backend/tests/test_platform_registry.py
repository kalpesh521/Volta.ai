"""
Feature flags (kill switches), the prompt registry, the model registry, and building the platform at startup.
"""
from __future__ import annotations

import pytest

from app.agents_platform.contracts import AgentName
from app.agents_platform.platform import build_platform
from app.agents_platform.registry import prompts as registry
from app.agents_platform.registry.flags import (
    KNOWN_FLAGS,
    LLM_FLAG,
    FeatureFlags,
    InMemoryFlagStore,
    RedisFlagStore,
    agent_flag,
    parse_disabled,
)
from app.agents_platform.registry.models import describe_models
from app.ai.config import AISettings, LLMRole
from app.modules.assistant import prompts
from tests.assistant_helpers import FakeStructuredLLM
from tests.redis_helpers import fresh_provider, needs_redis


def settings(**overrides) -> AISettings:
    return AISettings(_env_file=None, **overrides)


# --- flags ----------------------------------------------------------------------------------------


def test_every_agent_and_the_model_have_a_kill_switch():
    assert KNOWN_FLAGS == {LLM_FLAG, *(agent_flag(a) for a in AgentName)}
    assert agent_flag(AgentName.KNOWLEDGE) == "ai.agent.knowledge.enabled"


async def test_everything_is_on_by_default():
    flags = await FeatureFlags(InMemoryFlagStore()).snapshot()
    assert flags.llm_enabled and all(flags.agent_enabled(a) for a in AgentName)
    assert set(flags.as_dict()) == KNOWN_FLAGS and all(flags.as_dict().values())


def test_a_typo_in_the_environment_fails_startup():
    assert parse_disabled("ai.llm.enabled, ai.agent.knowledge.enabled") == {
        "ai.llm.enabled",
        "ai.agent.knowledge.enabled",
    }
    assert parse_disabled("") == frozenset()
    with pytest.raises(ValueError, match="ai.agent.knowlege.enabled"):
        parse_disabled("ai.agent.knowlege.enabled")


async def test_the_environment_baseline_switches_a_flag_off():
    flags = FeatureFlags(InMemoryFlagStore(), baseline_disabled=parse_disabled("ai.agent.knowledge.enabled"))
    snapshot = await flags.snapshot()
    assert not snapshot.agent_enabled(AgentName.KNOWLEDGE)
    assert snapshot.agent_enabled(AgentName.ENERGY_INSIGHT)


async def test_a_runtime_override_wins_over_the_environment_in_both_directions():
    flags = FeatureFlags(InMemoryFlagStore(), baseline_disabled=frozenset({LLM_FLAG}))
    assert not (await flags.snapshot()).llm_enabled

    await flags.set(LLM_FLAG, True)  # switch back on without a deploy
    assert (await flags.snapshot()).llm_enabled

    await flags.set(agent_flag(AgentName.DEVICE_ACTION), False)  # kill switch
    assert not (await flags.snapshot()).agent_enabled(AgentName.DEVICE_ACTION)

    await flags.clear(agent_flag(AgentName.DEVICE_ACTION))
    await flags.clear(LLM_FLAG)
    after = await flags.snapshot()
    assert after.agent_enabled(AgentName.DEVICE_ACTION) and not after.llm_enabled  # back to the baseline


async def test_an_unknown_flag_cannot_be_set():
    flags = FeatureFlags(InMemoryFlagStore())
    with pytest.raises(ValueError):
        await flags.set("ai.agent.warp_drive.enabled", False)
    with pytest.raises(ValueError):
        await flags.clear("nope")


async def test_a_snapshot_does_not_change_under_a_request():
    flags = FeatureFlags(InMemoryFlagStore())
    snapshot = await flags.snapshot()
    await flags.set(LLM_FLAG, False)
    assert snapshot.llm_enabled  # the request already holding it keeps its view


@needs_redis
async def test_flag_overrides_are_shared_through_redis():
    async with fresh_provider() as provider:
        pod_a = FeatureFlags(RedisFlagStore(provider))
        pod_b = FeatureFlags(RedisFlagStore(provider))
        await pod_a.set(agent_flag(AgentName.KNOWLEDGE), False)
        assert not (await pod_b.snapshot()).agent_enabled(AgentName.KNOWLEDGE)  # another pod sees it at once
        await pod_a.clear(agent_flag(AgentName.KNOWLEDGE))
        assert (await pod_b.snapshot()).agent_enabled(AgentName.KNOWLEDGE)


async def test_when_redis_is_down_the_environment_baseline_applies():
    from app.agents_platform.infra.redis import RedisProvider

    down = RedisProvider("redis://127.0.0.1:1/0", cooldown_seconds=30.0)
    flags = FeatureFlags(RedisFlagStore(down), baseline_disabled=frozenset({LLM_FLAG}))
    snapshot = await flags.snapshot()
    assert not snapshot.llm_enabled and snapshot.agent_enabled(AgentName.KNOWLEDGE)
    assert await flags.set(LLM_FLAG, True) is False  # reported, not silently dropped


# --- prompt registry ------------------------------------------------------------------------------


def test_the_default_answer_prompt_is_the_measured_one():
    assert registry.default_version(registry.ANSWER) == prompts.PROMPT_VERSION
    assert registry.resolve(registry.ANSWER) == prompts.PROMPT_VERSION
    assert registry.resolve(registry.ANSWER, "  ") == prompts.PROMPT_VERSION


def test_a_registered_version_can_be_selected_and_an_unknown_one_fails_loudly():
    assert registry.resolve(registry.ANSWER, prompts.PROMPT_VERSION_V5) == prompts.PROMPT_VERSION_V5
    with pytest.raises(registry.UnknownPromptVersionError, match="registered"):
        registry.resolve(registry.ANSWER, "2026-01-01.v0")


def test_an_unknown_prompt_version_stops_the_platform_at_startup():
    with pytest.raises(registry.UnknownPromptVersionError):
        build_platform(settings(AI_PROMPT_ANSWER_VERSION="nonsense"))


def test_v5_is_v4_plus_exactly_one_rule_about_untrusted_documents():
    v4 = prompts.ANSWER_PROMPTS[prompts.PROMPT_VERSION].messages[0].prompt.template
    v5 = prompts.ANSWER_PROMPTS[prompts.PROMPT_VERSION_V5].messages[0].prompt.template
    assert v5 != v4 and "untrusted data" in v5 and "untrusted data" not in v4
    assert v5.replace(prompts._DOCUMENT_RULE_V5, "") == v4  # nothing else changed


def test_the_active_version_reaches_the_messages_the_model_sees():
    kwargs = dict(question="q", intents=["battery"], facts={}, analytics={}, freshness={})
    default = registry.ResolvedPrompts().answer_messages(**kwargs)
    canary = registry.ResolvedPrompts(answer_version=prompts.PROMPT_VERSION_V5).answer_messages(**kwargs)
    assert "11." not in default[0].content and "11." in canary[0].content


def test_the_resolved_prompts_report_what_is_registered():
    resolved = build_platform(settings()).prompts.as_dict()
    assert resolved["answer"]["active"] == prompts.PROMPT_VERSION
    assert set(resolved["answer"]["registered"]) == {prompts.PROMPT_VERSION, prompts.PROMPT_VERSION_V5}


# --- model registry -------------------------------------------------------------------------------


def test_each_role_has_a_target_and_the_guard_falls_back_to_the_router_then_the_answer_model():
    base = settings(AI_LLM_PROVIDER="openrouter", AI_LLM_MODEL="answer-model")
    assert base.target(LLMRole.GUARD).model == "answer-model"
    with_router = settings(AI_LLM_MODEL="answer-model", AI_ROUTER_LLM_MODEL="router-model")
    assert with_router.target(LLMRole.GUARD).model == "router-model"
    with_guard = settings(AI_ROUTER_LLM_MODEL="router-model", AI_GUARD_LLM_MODEL="guard-model")
    assert with_guard.target(LLMRole.GUARD).model == "guard-model"
    assert with_guard.target(LLMRole.ROUTER).model == "router-model"  # the guard setting does not leak into routing


def test_describe_models_lists_every_role():
    entries = describe_models(settings(), FakeStructuredLLM(available=True))
    assert {e.role for e in entries} == {r.value for r in LLMRole}
    assert all(e.available for e in entries)
    assert not any(e.available for e in describe_models(settings(), FakeStructuredLLM(available=False)))


# --- the platform ---------------------------------------------------------------------------------


def test_the_redis_vault_needs_a_key():
    with pytest.raises(ValueError, match="AI_PII_VAULT_KEY"):
        build_platform(settings(AI_PII_VAULT_BACKEND="redis"))


def test_a_malformed_vault_key_stops_startup():
    with pytest.raises(ValueError):
        build_platform(settings(AI_PII_VAULT_BACKEND="redis", AI_PII_VAULT_KEY="c2hvcnQ="))


def test_a_bad_flag_name_stops_startup():
    with pytest.raises(ValueError, match="unknown feature flag"):
        build_platform(settings(AI_FLAGS_DISABLED="ai.agent.knowlege.enabled"))


def test_the_redis_backed_platform_builds_with_a_valid_key():
    from app.agents_platform.guardrails.pii.vault import RedisVault, generate_key
    from app.agents_platform.locks import RedisConversationLock

    platform = build_platform(
        settings(AI_PLATFORM_STORE="redis", AI_PII_VAULT_BACKEND="redis", AI_PII_VAULT_KEY=generate_key())
    )
    assert isinstance(platform.vault, RedisVault) and isinstance(platform.locks, RedisConversationLock)


def test_the_default_platform_needs_no_external_service():
    from app.agents_platform.guardrails.pii.vault import InMemoryVault
    from app.agents_platform.locks import InMemoryConversationLock

    platform = build_platform(settings())
    assert isinstance(platform.vault, InMemoryVault) and isinstance(platform.locks, InMemoryConversationLock)
