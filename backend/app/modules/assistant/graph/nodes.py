"""
LangGraph node implementations.

Each node reads a typed slice of `AssistantState`, returns only the keys it
owns, and gets request-scoped dependencies from `runtime.context`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.gateway import StructuredResult
from app.modules.assistant.domain.analytics import Analytic, AnalyticsBundle, Severity, run_analytics
from app.modules.assistant.domain.answers import AnswerDraft, FallbackReason, compose_fallback_answer
from app.modules.assistant.domain.context import AIContext, fit_to_budget
from app.modules.assistant.domain.freshness import DataFreshness, FreshnessStatus, assess_freshness
from app.modules.assistant.domain.projection import Detail, project_prompt
from app.modules.assistant.domain.intents import (
    ApplianceType,
    ClassificationMethod,
    Intent,
    IntentClassification,
    classify_by_rules,
    from_llm,
)
from app.modules.assistant.graph.planner import build_plan
from app.modules.assistant.graph.state import (
    AssistantRuntime,
    AssistantState,
    LLMCallRecord,
    ToolCallRecord,
)
from app.modules.assistant.prompts import answer_messages, router_messages
from app.modules.assistant.tools.registry import ToolName, is_available, unavailable

logger = logging.getLogger("volta.assistant.graph")

CLASSIFY = "classify"
SELECT_TOOLS = "select_tools"
CALL_TOOLS = "call_tools"
ANALYZE = "analyze"
GENERATE = "generate"
FALLBACK = "fallback"
FINALIZE = "finalize"

_CONTEXT_FIELDS: dict[ToolName, str] = {
    ToolName.HOUSEHOLD_PROFILE: "household",
    ToolName.LIVE_ENERGY_STATE: "live_energy",
    ToolName.BATTERY_STATUS: "battery",
    ToolName.GRID_STATUS: "grid",
    ToolName.DEVICE_READINGS: "devices",
    ToolName.WEATHER_DATA: "weather",
    ToolName.DAILY_ENERGY_SUMMARY: "today_summary",
    ToolName.HOURLY_ENERGY_SUMMARY: "hourly_summary",
    ToolName.RECENT_TREND: "recent_trend",
}

_BLOCKED: dict[Intent, FallbackReason] = {
    Intent.DEVICE_CONTROL: FallbackReason.DEVICE_CONTROL,
    Intent.OUT_OF_SCOPE: FallbackReason.OUT_OF_SCOPE,
}

_STALE_PREFIX = "Because this data is not current, confirm the live readings before acting. "


def _llm_record(role: LLMRole, result: StructuredResult[Any]) -> LLMCallRecord:
    return LLMCallRecord(
        role=role.value,
        model=result.model,
        latency_ms=result.latency_ms,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        total_tokens=result.usage.total_tokens,
    )


def _primary_intent(state: AssistantState) -> Intent:
    intents = state.get("intents") or [Intent.LIVE_OVERVIEW.value]
    return Intent(intents[0])


async def classify(
    state: AssistantState, config: RunnableConfig, runtime: Runtime[AssistantRuntime]
) -> dict[str, Any]:
    """Router cascade: rules first, router LLM only when rules are not confident."""
    question = state["question"]
    match = classify_by_rules(question)
    update: dict[str, Any] = {}
    if not match.confident:
        llm = runtime.context.llm
        if llm.is_available(LLMRole.ROUTER):
            try:
                result = await llm.ainvoke(
                    LLMRole.ROUTER, IntentClassification, router_messages(question), config=config
                )
                match = from_llm(result.output, match.appliance)
                update["llm_calls"] = [_llm_record(LLMRole.ROUTER, result)]
            except LLMInvocationError as exc:
                logger.warning("router LLM failed, using rules: %s", exc)
                update["errors"] = [str(exc)]
                match = replace(match, method=ClassificationMethod.DEFAULT)
        else:
            match = replace(match, method=ClassificationMethod.DEFAULT)
    return {
        **update,
        "intents": [intent.value for intent in match.intents],
        "appliance": match.appliance.value if match.appliance else None,
        "classification_method": match.method.value,
    }


def select_tools(state: AssistantState) -> dict[str, Any]:
    plan = build_plan(Intent(i) for i in state.get("intents", []))
    return {
        "planned_tools": [tool.value for tool in plan.tools],
        "planned_analytics": [analytic.value for analytic in plan.analytics],
        "planned_details": [detail.value for detail in plan.details],
        "requires_telemetry": plan.requires_telemetry,
        "live_advice": plan.live_advice,
    }


def route_after_select(state: AssistantState) -> str:
    return CALL_TOOLS if state.get("planned_tools") else ANALYZE


async def call_tools(
    state: AssistantState, config: RunnableConfig, runtime: Runtime[AssistantRuntime]
) -> dict[str, Any]:
    """Fan out to the planned tools concurrently; a slow or failing tool degrades to unavailable."""
    tools = runtime.context.tools
    timeout = runtime.context.settings.AI_TOOL_TIMEOUT_SECONDS

    async def run(name: str) -> tuple[str, dict[str, Any], ToolCallRecord]:
        started = time.perf_counter()
        status: str
        try:
            result = await asyncio.wait_for(tools[ToolName(name)].ainvoke({}, config=config), timeout)
            status = "ok" if is_available(result) else "unavailable"
        except TimeoutError:
            result, status = unavailable("Timed out while loading this data."), "timeout"
        except Exception:
            logger.exception("tool %s failed", name)
            result, status = unavailable("This data could not be loaded."), "error"
        latency = int((time.perf_counter() - started) * 1000)
        return name, result, ToolCallRecord(name=name, status=status, latency_ms=latency)

    outcomes = await asyncio.gather(*(run(name) for name in state.get("planned_tools", [])))
    return {
        "tool_results": {name: result for name, result, _ in outcomes},
        "tool_calls": [record for _, _, record in outcomes],
    }


def assemble_context(tool_results: dict[str, dict[str, Any]]) -> AIContext:
    payload = {
        field: tool_results[tool.value]
        for tool, field in _CONTEXT_FIELDS.items()
        if is_available(tool_results.get(tool.value))
    }
    context = AIContext.model_validate(payload)
    if context.household is not None:
        context.preferences = {
            "primary_goal": context.household.primary_goal,
            "device_control_enabled": False,
        }
    return context


def analyze(state: AssistantState, runtime: Runtime[AssistantRuntime]) -> dict[str, Any]:
    """Deterministic analytics + freshness guard, then pick the answer path."""
    ctx = runtime.context
    context = assemble_context(state.get("tool_results", {}))
    freshness = assess_freshness(
        context.data_time,
        now=ctx.now(),
        threshold_seconds=ctx.settings.AI_STALE_DATA_SECONDS,
    )
    appliance = ApplianceType(state["appliance"]) if state.get("appliance") else None
    bundle = run_analytics(
        [Analytic(a) for a in state.get("planned_analytics", [])], context, appliance=appliance
    )

    live_advice = state.get("live_advice", True)
    warnings: list[str] = []
    if freshness.status is FreshnessStatus.STALE and freshness.message and live_advice:
        warnings.append(freshness.message)
    failed = [c["name"] for c in state.get("tool_calls", []) if c["status"] in ("timeout", "error")]
    if failed:
        warnings.append(f"Some data could not be loaded: {', '.join(failed)}.")
    warnings.extend(a.message for a in bundle.anomalies or [] if a.severity is Severity.CRITICAL)

    primary = _primary_intent(state)
    reason: FallbackReason | None = _BLOCKED.get(primary)
    if reason is None and freshness.status is FreshnessStatus.MISSING and (
        state.get("requires_telemetry", True) or context.household is None
    ):
        reason = FallbackReason.NO_DATA
    if reason is None and not ctx.llm.is_available(LLMRole.ANSWER):
        reason = FallbackReason.LLM_UNAVAILABLE

    full_payload = context.prompt_payload()
    analytics_payload = bundle.prompt_payload()
    freshness_payload = freshness.model_dump(mode="json")
    prompt_facts, prompt_analytics, prompt_freshness = project_prompt(
        full_payload,
        analytics_payload,
        freshness_payload,
        {Detail(d) for d in state.get("planned_details", [])},
    )
    prompt_facts, reductions = fit_to_budget(prompt_facts, ctx.settings.AI_MAX_FACTS_CHARS)
    if reductions:
        logger.info("facts trimmed to fit budget: %s", ",".join(reductions))

    return {
        "context": full_payload,
        "prompt_context": prompt_facts,
        "prompt_analytics": prompt_analytics,
        "prompt_freshness": prompt_freshness,
        "context_reductions": reductions,
        "analytics": {
            "results": analytics_payload,
            "computed": [a.value for a in bundle.computed],
        },
        "freshness": freshness_payload,
        "warnings": warnings,
        "route": FALLBACK if reason else GENERATE,
        "fallback_reason": reason.value if reason else None,
    }


def route_after_analyze(state: AssistantState) -> str:
    return state.get("route", FALLBACK)


async def generate(
    state: AssistantState, config: RunnableConfig, runtime: Runtime[AssistantRuntime]
) -> dict[str, Any]:
    """Single structured-output LLM call that explains the precomputed facts."""
    messages = answer_messages(
        question=state["question"],
        intents=state.get("intents", []),
        facts=state.get("prompt_context") or state.get("context", {}),
        analytics=state.get("prompt_analytics") or state.get("analytics", {}).get("results", {}),
        freshness=state.get("prompt_freshness") or state.get("freshness", {}),
    )
    try:
        result = await runtime.context.llm.ainvoke(
            LLMRole.ANSWER, AnswerDraft, messages, config=config
        )
    except (LLMInvocationError, LLMNotConfiguredError) as exc:
        logger.warning("answer LLM failed, using deterministic answer: %s", exc)
        return {
            "route": FALLBACK,
            "fallback_reason": FallbackReason.LLM_ERROR.value,
            "errors": [str(exc)],
        }
    return {
        "answer": result.output.model_dump(),
        "llm_used": True,
        "llm_calls": [_llm_record(LLMRole.ANSWER, result)],
        "route": FINALIZE,
    }


def route_after_generate(state: AssistantState) -> str:
    return FALLBACK if state.get("route") == FALLBACK else FINALIZE


def fallback(state: AssistantState) -> dict[str, Any]:
    reason = FallbackReason(state.get("fallback_reason") or FallbackReason.LLM_UNAVAILABLE.value)
    draft = compose_fallback_answer(
        reason=reason,
        intents=[Intent(i) for i in state.get("intents", [])],
        context=AIContext.model_validate(state.get("context", {})),
        analytics=AnalyticsBundle.model_validate(state.get("analytics", {}).get("results", {})),
    )
    return {"answer": draft.model_dump(), "llm_used": False}


def _confidence(state: AssistantState, freshness: DataFreshness) -> str:
    reason = state.get("fallback_reason")
    if reason in (FallbackReason.DEVICE_CONTROL.value, FallbackReason.OUT_OF_SCOPE.value):
        return "high"
    if freshness.status is not FreshnessStatus.FRESH:
        if state.get("requires_telemetry", True):
            return "low"
        # Setup answers come from onboarding; only the live part is uncertain.
        if state.get("live_advice", True):
            return "medium"
    anomalies = state.get("analytics", {}).get("results", {}).get("anomalies", [])
    data_issue = any(
        a.get("code") in {"data_quality_degraded", "energy_balance_mismatch"} for a in anomalies
    )
    tool_issue = any(c["status"] in ("timeout", "error") for c in state.get("tool_calls", []))
    return "medium" if data_issue or tool_issue else "high"


def finalize(state: AssistantState) -> dict[str, Any]:
    """Guardrails the LLM cannot override: data time, stale-data caution, confidence."""
    freshness = DataFreshness.model_validate(state["freshness"])
    answer = dict(state.get("answer") or {})
    if freshness.status is FreshnessStatus.STALE and state.get("live_advice", True):
        recommendation = answer.get("recommendation", "")
        if not recommendation.startswith(_STALE_PREFIX):
            answer["recommendation"] = _STALE_PREFIX + recommendation
    answer["data_time"] = (
        freshness.data_time.isoformat() if freshness.data_time else "unavailable"
    )
    return {"answer": answer, "confidence": _confidence(state, freshness)}
