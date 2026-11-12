"""
Deterministic stand-ins for the model and the specialist agents.

The trajectory evals and the graph tests use them to exercise the *supervisor*: routing, dispatch,
deadlines, regeneration, memory. Nothing here touches a network or a database, so a run takes
milliseconds and its result never depends on a model's mood.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from app.agents_platform.contracts import (
    AgentCall,
    AgentName,
    AgentResult,
    AgentStatus,
    AnswerParts,
)
from app.agents_platform.guardrails.classifier import InjectionVerdict
from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError
from app.ai.llm.gateway import StructuredResult, TokenUsage
from app.modules.assistant.domain.answers import AnswerDraft
from app.modules.assistant.domain.intents import Intent, IntentClassification

CLEAN_DRAFT = AnswerDraft(
    observation="Stub observation.",
    explanation="Stub explanation.",
    recommendation="Stub recommendation.",
    estimated_impact="Stub impact.",
)


class ScriptedLLM:
    """
    A `StructuredLLM` that replays canned answers.

    `answers` are returned in order, the last one repeating. An item that is an exception is raised
    instead, which is how a test makes the model fail on one specific attempt.
    """

    def __init__(
        self,
        answers: Sequence[AnswerDraft | Exception] = (CLEAN_DRAFT,),
        *,
        classification: IntentClassification | None = None,
        verdict: InjectionVerdict | Exception | None = None,
        available: bool = True,
    ) -> None:
        self._answers = list(answers)
        self._verdict = verdict or InjectionVerdict(is_injection=False)
        self._classification = classification or IntentClassification(primary_intent=Intent.OUT_OF_SCOPE)
        self._available = available
        self.calls: list[tuple[LLMRole, list[BaseMessage]]] = []

    def is_available(self, role: LLMRole) -> bool:
        return self._available

    def count(self, role: LLMRole) -> int:
        return sum(1 for r, _ in self.calls if r is role)

    def prompts(self, role: LLMRole) -> list[str]:
        return ["\n".join(str(m.content) for m in msgs) for r, msgs in self.calls if r is role]

    async def ainvoke(
        self, role: LLMRole, schema: type, messages: Sequence[BaseMessage], *, config: Any = None
    ) -> StructuredResult:
        self.calls.append((role, list(messages)))
        usage = TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        if schema is InjectionVerdict:
            if isinstance(self._verdict, Exception):
                raise self._verdict
            return StructuredResult(output=self._verdict, model="stub-guard", usage=usage, latency_ms=1)
        if schema is IntentClassification:
            return StructuredResult(output=self._classification, model="stub-router", usage=usage, latency_ms=1)
        index = min(self.count(LLMRole.ANSWER) - 1, len(self._answers) - 1)
        item = self._answers[index]
        if isinstance(item, Exception):
            raise item
        return StructuredResult(output=item, model="stub-answer", usage=usage, latency_ms=1)


def llm_failure() -> LLMInvocationError:
    return LLMInvocationError("answer model call failed (TimeoutError)")


class StubAgent:
    """A specialist that returns a canned result, optionally slowly or by raising."""

    def __init__(
        self,
        name: AgentName,
        *,
        facts: dict[str, Any] | None = None,
        draft: AnswerParts | None = None,
        fallback_reason: str | None = None,
        status: AgentStatus = AgentStatus.OK,
        delay: float = 0.0,
        error: Exception | None = None,
        build: Callable[[AgentCall], AgentResult] | None = None,
    ) -> None:
        self.name = name
        self._facts = facts if facts is not None else {"marker": name.value}
        self._draft = draft or AnswerParts(**CLEAN_DRAFT.model_dump())
        self._fallback_reason = fallback_reason
        self._status = status
        self._delay = delay
        self._error = error
        self._build = build
        self.calls: list[AgentCall] = []

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult:
        self.calls.append(call)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if self._build is not None:
            return self._build(call)
        return AgentResult(
            agent=self.name,
            status=self._status,
            facts=self._facts,
            draft=self._draft,
            fallback_reason=self._fallback_reason,
            confidence="high",
            live_advice=False,
        )


def default_stub_agents() -> dict[AgentName, StubAgent]:
    """One healthy stub per agent. Device action answers by itself, as the real one does."""
    return {
        AgentName.ENERGY_INSIGHT: StubAgent(AgentName.ENERGY_INSIGHT),
        AgentName.KNOWLEDGE: StubAgent(AgentName.KNOWLEDGE),
        AgentName.DEVICE_ACTION: StubAgent(AgentName.DEVICE_ACTION, fallback_reason="device_action"),
    }
