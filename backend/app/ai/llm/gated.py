"""
A `StructuredLLM` that can be switched off for one request.

Used for two cases that must behave the same way: the `ai.llm.enabled` kill switch, and a user whose daily token
budget is spent. A gated model reports itself unavailable and refuses every call with `LLMNotConfiguredError`, which
the router and the synthesizer already treat as "answer from verified facts without the model". No node needs to
know *why* the model is off.
"""
from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from app.ai.config import LLMRole
from app.ai.errors import LLMNotConfiguredError
from app.ai.llm.gateway import StructuredLLM, StructuredResult, T


class GatedLLM:
    def __init__(self, inner: StructuredLLM, *, reason: str) -> None:
        self._inner = inner
        self._reason = reason

    def is_available(self, role: LLMRole) -> bool:
        return False

    async def ainvoke(
        self,
        role: LLMRole,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        config: RunnableConfig | None = None,
    ) -> StructuredResult[T]:
        raise LLMNotConfiguredError(f"the model is switched off for this request ({self._reason})")
