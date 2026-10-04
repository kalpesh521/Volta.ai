"""
Structured-output LLM port.

Graph nodes depend on the `StructuredLLM` protocol, not on LangChain, so tests
inject a fake and providers can change without touching workflow code.
`LangChainStructuredLLM` is the production adapter: provider-native structured
output, optional fallback model, token-usage capture.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel

from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.factory import ChatModelFactory

logger = logging.getLogger("volta.ai.llm")

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    output: T
    model: str
    usage: TokenUsage
    latency_ms: int


class StructuredLLM(Protocol):
    def is_available(self, role: LLMRole) -> bool: ...

    async def ainvoke(
        self,
        role: LLMRole,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        config: RunnableConfig | None = None,
    ) -> StructuredResult[T]: ...


def _usage(raw: Any) -> TokenUsage:
    meta = getattr(raw, "usage_metadata", None) or {}
    return TokenUsage(
        input_tokens=int(meta.get("input_tokens") or 0),
        output_tokens=int(meta.get("output_tokens") or 0),
        total_tokens=int(meta.get("total_tokens") or 0),
    )


def _model_name(raw: Any, default: str) -> str:
    if isinstance(raw, AIMessage):
        meta = raw.response_metadata or {}
        name = meta.get("model_name") or meta.get("model")
        if name:
            return str(name)
    return default


class LangChainStructuredLLM:
    def __init__(self, factory: ChatModelFactory) -> None:
        self._factory = factory
        self._chains: dict[tuple[LLMRole, type[BaseModel]], Runnable] = {}

    def is_available(self, role: LLMRole) -> bool:
        try:
            self._factory.primary(role)
        except LLMNotConfiguredError as exc:
            logger.info("LLM role=%s unavailable: %s", role.value, exc)
            return False
        return True

    def _chain(self, role: LLMRole, schema: type[BaseModel]) -> Runnable:
        key = (role, schema)
        chain = self._chains.get(key)
        if chain is not None:
            return chain
        primary = self._factory.primary(role).with_structured_output(schema, include_raw=True)
        fallback_model = self._factory.fallback()
        if fallback_model is not None:
            chain = primary.with_fallbacks(
                [fallback_model.with_structured_output(schema, include_raw=True)]
            )
        else:
            chain = primary
        self._chains[key] = chain
        return chain

    async def ainvoke(
        self,
        role: LLMRole,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        config: RunnableConfig | None = None,
    ) -> StructuredResult[T]:
        chain = self._chain(role, schema)
        started = time.perf_counter()
        try:
            result = await chain.ainvoke(list(messages), config=config)
        except LLMNotConfiguredError:
            raise
        except Exception as exc:
            raise LLMInvocationError(
                f"{role.value} model call failed ({type(exc).__name__})"
            ) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        raw = result.get("raw") if isinstance(result, dict) else None
        parsed = result.get("parsed") if isinstance(result, dict) else result
        if isinstance(result, dict) and result.get("parsing_error") is not None:
            raise LLMInvocationError(f"{role.value} model returned unparseable output")
        if parsed is None:
            raise LLMInvocationError(f"{role.value} model returned no structured output")
        if isinstance(parsed, dict):
            parsed = schema.model_validate(parsed)

        return StructuredResult(
            output=parsed,
            model=_model_name(raw, self._factory.target(role).model),
            usage=_usage(raw),
            latency_ms=latency_ms,
        )
