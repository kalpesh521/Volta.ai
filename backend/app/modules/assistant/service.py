"""
Assistant use case: bind tools to the caller's household, run the graph,
map the final state to the API contract, and emit one structured log line.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.ai.config import AISettings, LLMRole
from app.ai.llm.factory import ChatModelFactory
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.graph.state import AssistantRuntime, AssistantState
from app.modules.assistant.prompts import PROMPT_VERSION
from app.modules.assistant.schemas import (
    AssistantAnswer,
    AssistantMeta,
    AssistantResponse,
    AssistantStatusOut,
    FreshnessOut,
    ModelStatusOut,
    ToolCallOut,
    UsageOut,
)
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.assistant.tools.registry import build_energy_tools
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem

logger = logging.getLogger("volta.assistant")

_RECURSION_LIMIT = 20


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssistantService:
    def __init__(
        self,
        *,
        energy: EnergyService,
        llm: StructuredLLM,
        graph: CompiledStateGraph,
        settings: AISettings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._energy = energy
        self._llm = llm
        self._graph = graph
        self._settings = settings
        self._clock = clock

    async def ask(self, system: SolarSystem, question: str, *, request_id: str) -> AssistantResponse:
        gateway = EnergyDataGateway(self._energy, system)
        runtime = AssistantRuntime(
            tools=build_energy_tools(
                gateway,
                trend_window_minutes=self._settings.AI_TREND_WINDOW_MINUTES,
                trend_history_limit=self._settings.AI_TREND_HISTORY_LIMIT,
            ),
            llm=self._llm,
            settings=self._settings,
            now=self._clock,
        )
        started = time.perf_counter()
        state: AssistantState = await self._graph.ainvoke(
            {"question": question, "request_id": request_id},
            context=runtime,
            config={
                "run_name": "suryaa_assistant",
                "tags": ["assistant", "read-only"],
                "metadata": {
                    "request_id": request_id,
                    "household_id": system.household_id,
                    "prompt_version": PROMPT_VERSION,
                },
                "recursion_limit": _RECURSION_LIMIT,
            },
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        response = to_response(
            household_id=system.household_id,
            question=question,
            state=state,
            request_id=request_id,
            latency_ms=latency_ms,
        )
        logger.info(
            "assistant request_id=%s household=%s intents=%s tools=%s llm_used=%s "
            "fallback=%s confidence=%s tokens=%d latency_ms=%d question_chars=%d",
            request_id,
            system.household_id,
            ",".join(response.intents),
            ",".join(response.tools_used),
            response.meta.llm_used,
            response.meta.fallback_reason,
            response.confidence,
            response.meta.usage.total_tokens,
            latency_ms,
            len(question),
        )
        return response


def to_response(
    *,
    household_id: str,
    question: str,
    state: AssistantState,
    request_id: str,
    latency_ms: int,
) -> AssistantResponse:
    llm_calls = state.get("llm_calls", [])
    usage = UsageOut(
        input_tokens=sum(c["input_tokens"] for c in llm_calls),
        output_tokens=sum(c["output_tokens"] for c in llm_calls),
        total_tokens=sum(c["total_tokens"] for c in llm_calls),
    )
    answer_model = next(
        (c["model"] for c in reversed(llm_calls) if c["role"] == LLMRole.ANSWER.value), None
    )
    freshness: dict[str, Any] = state.get("freshness", {})
    tool_calls = [ToolCallOut.model_validate(c) for c in state.get("tool_calls", [])]
    llm_used = bool(state.get("llm_used"))
    return AssistantResponse(
        household_id=household_id,
        question=question,
        answer=AssistantAnswer.model_validate(state["answer"]),
        confidence=state.get("confidence", "low"),
        warnings=list(dict.fromkeys(state.get("warnings", []))),
        intents=state.get("intents", []),
        appliance=state.get("appliance"),
        tools_used=[c.name for c in tool_calls if c.status == "ok"],
        analytics_used=state.get("analytics", {}).get("computed", []),
        data_freshness=FreshnessOut(
            status=freshness.get("status", "missing"),
            data_time=freshness.get("data_time"),
            age_seconds=freshness.get("age_seconds"),
            threshold_seconds=freshness.get("threshold_seconds", 0),
        ),
        meta=AssistantMeta(
            request_id=request_id,
            llm_used=llm_used,
            model=answer_model if llm_used else None,
            classification_method=state.get("classification_method", "rules"),
            fallback_reason=None if llm_used else state.get("fallback_reason"),
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            usage=usage,
            tool_calls=tool_calls,
        ),
    )


def assistant_status(settings: AISettings, llm: StructuredLLM, factory: ChatModelFactory) -> AssistantStatusOut:
    answer_target = settings.target(LLMRole.ANSWER)
    router_target = settings.target(LLMRole.ROUTER)
    fallback_target = settings.fallback_target()
    answer_ok = llm.is_available(LLMRole.ANSWER)
    return AssistantStatusOut(
        enabled=settings.AI_ENABLED,
        mode="llm" if answer_ok else "deterministic",
        answer_model=ModelStatusOut(**answer_target.model_dump(), available=answer_ok),
        router_model=ModelStatusOut(
            **router_target.model_dump(), available=llm.is_available(LLMRole.ROUTER)
        ),
        fallback_model=(
            ModelStatusOut(**fallback_target.model_dump(), available=factory.fallback() is not None)
            if fallback_target
            else None
        ),
        prompt_version=PROMPT_VERSION,
    )
