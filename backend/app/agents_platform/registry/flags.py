"""
Feature flags and kill switches (doc 04 §6).

    ai.llm.enabled               off → every answer is the deterministic one built from verified facts
    ai.agent.<name>.enabled      off → that agent is not run; the answer says its information is unavailable

A flag is on unless it is switched off. There are two places to switch one off:

* `AI_FLAGS_DISABLED` (environment): the baseline for a deployment.
* a runtime override (Redis, through `PUT /v2/assistant/admin/flags/{flag}`): flips a flag on every API pod at
  once, with no deploy and no restart. An override wins over the environment, in both directions.

The flags are read once per request into an immutable `FlagSnapshot`, so a request never sees a flag change
halfway through. Flag names are a closed set: an unknown name in the environment fails startup, and an unknown
name through the API is refused, so a typo cannot silently fail to switch anything off.

If Redis is down the environment baseline applies. A kill switch that cannot be read must not take the
assistant down with it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from app.agents_platform.contracts import AgentName
from app.agents_platform.infra.redis import KEY_PREFIX, RedisProvider

logger = logging.getLogger("volta.agents.flags")

LLM_FLAG = "ai.llm.enabled"


def agent_flag(agent: AgentName) -> str:
    return f"ai.agent.{agent.value}.enabled"


KNOWN_FLAGS: frozenset[str] = frozenset({LLM_FLAG, *(agent_flag(a) for a in AgentName)})


def parse_disabled(csv: str) -> frozenset[str]:
    """Flags named in `AI_FLAGS_DISABLED`. Raises on an unknown name, so a typo fails startup."""
    names = frozenset(part.strip() for part in csv.split(",") if part.strip())
    unknown = names - KNOWN_FLAGS
    if unknown:
        raise ValueError(f"unknown feature flag(s) in AI_FLAGS_DISABLED: {', '.join(sorted(unknown))}")
    return names


@dataclass(frozen=True)
class FlagSnapshot:
    disabled: frozenset[str] = frozenset()

    def enabled(self, flag: str) -> bool:
        return flag not in self.disabled

    @property
    def llm_enabled(self) -> bool:
        return self.enabled(LLM_FLAG)

    def agent_enabled(self, agent: AgentName) -> bool:
        return self.enabled(agent_flag(agent))

    def as_dict(self) -> dict[str, bool]:
        return {flag: self.enabled(flag) for flag in sorted(KNOWN_FLAGS)}


class FlagStore(Protocol):
    async def overrides(self) -> dict[str, bool] | None:
        """Runtime overrides, or None when the store cannot be read."""

    async def set(self, flag: str, enabled: bool) -> bool: ...

    async def clear(self, flag: str) -> bool: ...


class InMemoryFlagStore:
    """Per process: a change reaches only this API pod. Development and tests."""

    def __init__(self) -> None:
        self._values: dict[str, bool] = {}

    async def overrides(self) -> dict[str, bool] | None:
        return dict(self._values)

    async def set(self, flag: str, enabled: bool) -> bool:
        self._values[flag] = enabled
        return True

    async def clear(self, flag: str) -> bool:
        self._values.pop(flag, None)
        return True


class RedisFlagStore:
    _KEY = f"{KEY_PREFIX}flags"

    def __init__(self, provider: RedisProvider) -> None:
        self._provider = provider

    async def overrides(self) -> dict[str, bool] | None:
        client = await self._provider.client()
        if client is None:
            return None
        try:
            raw = await client.hgetall(self._KEY)
        except Exception:
            logger.warning("flag store read failed; using the environment baseline", exc_info=True)
            self._provider.mark_down()
            return None
        return {flag: value == "1" for flag, value in raw.items() if flag in KNOWN_FLAGS}

    async def set(self, flag: str, enabled: bool) -> bool:
        client = await self._provider.client()
        if client is None:
            return False
        try:
            await client.hset(self._KEY, flag, "1" if enabled else "0")
        except Exception:
            self._provider.mark_down()
            return False
        return True

    async def clear(self, flag: str) -> bool:
        client = await self._provider.client()
        if client is None:
            return False
        try:
            await client.hdel(self._KEY, flag)
        except Exception:
            self._provider.mark_down()
            return False
        return True


class FeatureFlags:
    def __init__(self, store: FlagStore, *, baseline_disabled: frozenset[str] = frozenset()) -> None:
        self._store = store
        self._baseline = baseline_disabled

    async def snapshot(self) -> FlagSnapshot:
        disabled = set(self._baseline)
        overrides = await self._store.overrides() or {}
        for flag, enabled in overrides.items():
            (disabled.discard if enabled else disabled.add)(flag)
        return FlagSnapshot(frozenset(disabled))

    async def set(self, flag: str, enabled: bool) -> bool:
        self._require_known(flag)
        return await self._store.set(flag, enabled)

    async def clear(self, flag: str) -> bool:
        self._require_known(flag)
        return await self._store.clear(flag)

    @staticmethod
    def _require_known(flag: str) -> None:
        if flag not in KNOWN_FLAGS:
            raise ValueError(f"unknown feature flag: {flag}")
