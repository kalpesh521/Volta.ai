"""
Facts pipeline shared by the Energy Insight and Knowledge agents.

It is the v1 assistant graph cut off after `analyze`:

    select_tools → call_tools → analyze → END

The nodes are the v1 nodes, unchanged, so tool selection, analytics, freshness, projection and the
facts budget behave exactly as in v1. What differs is who writes the answer: the engine stops
before the LLM, and the supervisor's synthesizer does the one generation (and the grounding check,
the regeneration and the fallback) for every plan.

An agent is a plan profile over this engine: Energy Insight runs the energy intents, Knowledge
runs `document`. Each has its own deadline, result mapping and eval set.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents_platform.contracts import (
    AgentCall,
    AgentName,
    AgentResult,
    AgentStatus,
    AnswerParts,
)
from app.ai.config import AISettings
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.domain.analytics import AnalyticsBundle
from app.modules.assistant.domain.answers import FallbackReason, compose_fallback_answer
from app.modules.assistant.domain.context import AIContext
from app.modules.assistant.domain.finalize import assess_confidence
from app.modules.assistant.domain.freshness import DataFreshness
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.graph import nodes
from app.modules.assistant.graph.state import AssistantRuntime, AssistantState
from app.modules.assistant.tools.registry import ToolName

logger = logging.getLogger("volta.agents.insight")

_GRAPH_NAME = "suryaa_insight_facts"


def build_insight_graph() -> CompiledStateGraph:
    graph = StateGraph(AssistantState, context_schema=AssistantRuntime)
    graph.add_node(nodes.SELECT_TOOLS, nodes.select_tools)
    graph.add_node(nodes.CALL_TOOLS, nodes.call_tools)
    graph.add_node(nodes.ANALYZE, nodes.analyze)
    graph.add_edge(START, nodes.SELECT_TOOLS)
    graph.add_conditional_edges(
        nodes.SELECT_TOOLS,
        nodes.route_after_select,
        {nodes.CALL_TOOLS: nodes.CALL_TOOLS, nodes.ANALYZE: nodes.ANALYZE},
    )
    graph.add_edge(nodes.CALL_TOOLS, nodes.ANALYZE)
    graph.add_edge(nodes.ANALYZE, END)
    return graph.compile(name=_GRAPH_NAME)


_INSIGHT_GRAPH: CompiledStateGraph | None = None


def insight_graph() -> CompiledStateGraph:
    """Compiled once per process."""
    global _INSIGHT_GRAPH
    if _INSIGHT_GRAPH is None:
        _INSIGHT_GRAPH = build_insight_graph()
    return _INSIGHT_GRAPH


ToolsFor = Callable[[str], Mapping[ToolName, BaseTool]]


class InsightEngine:
    """
    Request-scoped: tools are bound to one household, so an engine is never shared across requests.

    `tools_for(question)` builds the tool set for one agent call. The question is a parameter because
    document search runs on the question as the supervisor resolved it (a follow-up carries the
    question it follows), not on the raw text the user typed.
    """

    def __init__(
        self,
        *,
        tools_for: ToolsFor,
        llm: StructuredLLM,
        settings: AISettings,
        now: Callable[[], datetime],
    ) -> None:
        self._tools_for = tools_for
        self._llm = llm
        self._settings = settings
        self._now = now

    async def gather(
        self, call: AgentCall, *, request_id: str, config: RunnableConfig | None = None
    ) -> AssistantState:
        state: AssistantState = {
            "question": call.question,
            "request_id": request_id,
            "intents": list(call.intents),
            "appliance": call.appliance,
            "classification_method": "supervisor",
        }
        runtime = AssistantRuntime(
            tools=self._tools_for(call.question), llm=self._llm, settings=self._settings, now=self._now
        )
        return await insight_graph().ainvoke(
            state, context=runtime, config={**(config or {}), "recursion_limit": 12}
        )


class InsightAgent:
    """Base for agents that gather facts through the engine and map them to an `AgentResult`."""

    name: AgentName

    def __init__(self, engine: InsightEngine, *, request_id: str) -> None:
        self._engine = engine
        self._request_id = request_id

    async def run(self, call: AgentCall, *, config: RunnableConfig | None = None) -> AgentResult:
        started = time.perf_counter()
        state = await self._engine.gather(call, request_id=self._request_id, config=config)
        return result_from_state(
            self.name, state, latency_ms=int((time.perf_counter() - started) * 1000)
        )


def result_from_state(agent: AgentName, state: AssistantState, *, latency_ms: int) -> AgentResult:
    """Map the engine's final state to the agent contract."""
    reason_value = state.get("fallback_reason")
    reason = FallbackReason(reason_value) if reason_value else None
    freshness = DataFreshness.model_validate(state["freshness"])
    analytics_results = state.get("analytics", {}).get("results", {})
    intents = [Intent(i) for i in state.get("intents", [])]

    # A note-free draft when the LLM is still expected to write the answer. The synthesizer adds
    # the right note if it ends up using this draft.
    draft = compose_fallback_answer(
        reason=reason or FallbackReason.LLM_UNAVAILABLE,
        intents=intents,
        context=AIContext.model_validate(state.get("context", {})),
        analytics=AnalyticsBundle.model_validate(analytics_results),
        note=None if reason else "",
    )

    tool_calls = state.get("tool_calls", [])
    degraded = any(c["status"] in ("timeout", "error") for c in tool_calls)
    status = AgentStatus.PARTIAL if degraded or reason is FallbackReason.NO_DATA else AgentStatus.OK
    live_advice = state.get("live_advice", True)
    requires_telemetry = state.get("requires_telemetry", True)

    passages = ((state.get("context") or {}).get("knowledge") or {}).get("passages") or []
    return AgentResult(
        agent=agent,
        status=status,
        facts=state.get("prompt_context") or state.get("context", {}),
        analytics=state.get("prompt_analytics") or analytics_results,
        freshness=state.get("freshness", {}),
        analytics_used=state.get("analytics", {}).get("computed", []),
        citations=list(passages),
        draft=AnswerParts.model_validate(draft.model_dump()),
        fallback_reason=reason.value if reason else None,
        warnings=list(state.get("warnings", [])),
        confidence=assess_confidence(
            fallback_reason=reason.value if reason else None,
            freshness=freshness,
            requires_telemetry=requires_telemetry,
            live_advice=live_advice,
            anomalies=analytics_results.get("anomalies", []),
            tool_statuses=[c["status"] for c in tool_calls],
        ),
        live_advice=live_advice,
        requires_telemetry=requires_telemetry,
        tool_calls=list(tool_calls),
        llm_calls=list(state.get("llm_calls", [])),
        latency_ms=latency_ms,
    )
