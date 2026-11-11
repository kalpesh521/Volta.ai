"""
Supervisor graph nodes.

    begin_turn → classify → plan ─┬→ <agent> ×N (parallel, via Send) → join → synthesize → output_gate → finalize
                                  ├→ <device_action> (exclusive)  ───────┘                     ↑      │
                                  └→ refuse ───────────────────────────────────────────────────┼──────┤
                                                                      regenerate once ←────────┘      │
                                                                      deterministic_fallback ─────────┘

Each node reads a slice of `SupervisorState` and returns only the keys it owns. Agent nodes are built
from one factory so deadlines, failure isolation and metrics are identical for every agent.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.agents_platform.contracts import (
    AgentCall,
    AgentName,
    AgentResult,
    AgentStatus,
    AnswerParts,
    Plan,
)
from app.agents_platform.orchestration.events import status_event
from app.agents_platform.orchestration.memory import resolve_follow_up
from app.agents_platform.orchestration.planner import build_agent_plan
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.state import AgentInput, SupervisorState, Turn, per_turn_reset
from app.agents_platform.orchestration.synthesis import (
    all_citations,
    combine_confidence,
    merge_drafts,
    merge_freshness,
    prompt_payload,
    unavailable_answer,
    usable,
)
from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.core.metrics import AGENT_RUNS, AGENT_SECONDS, GROUNDING_CHECKS, SUPERVISOR_REGENERATIONS
from app.modules.assistant.domain.answers import AnswerDraft, FallbackReason, compose_fallback_answer
from app.modules.assistant.domain.analytics import AnalyticsBundle
from app.modules.assistant.domain.context import AIContext
from app.modules.assistant.domain.finalize import finalize_answer
from app.modules.assistant.domain.grounding import check_grounding
from app.modules.assistant.domain.intents import Intent, classify_by_rules
from app.modules.assistant.graph.routing import route_question
from app.modules.assistant.graph.state import llm_record
from app.modules.assistant.prompts import answer_messages

logger = logging.getLogger("volta.agents.supervisor")

BEGIN_TURN = "begin_turn"
CLASSIFY = "classify"
PLAN = "plan"
JOIN = "join"
SYNTHESIZE = "synthesize"
OUTPUT_GATE = "output_gate"
FALLBACK = "deterministic_fallback"
REFUSE = "refuse"
FINALIZE = "finalize"

_STAGE_MESSAGES: dict[AgentName, str] = {
    AgentName.ENERGY_INSIGHT: "Checking your home's energy data…",
    AgentName.KNOWLEDGE: "Searching your documents…",
    AgentName.DEVICE_ACTION: "Checking whether that is safe to do…",
}


def _emit(runtime: Runtime[SupervisorRuntime], event: dict[str, Any]) -> None:
    """Send a progress event to a streaming client. A no-op when nobody is listening."""
    runtime.stream_writer(event)


def _results(state: SupervisorState) -> list[AgentResult]:
    """Agent results in plan order. Parallel branches finish in any order; the answer must not depend on it."""
    plan = Plan.model_validate(state.get("plan") or {})
    order = {call.agent: index for index, call in enumerate(plan.calls)}
    parsed = [AgentResult.model_validate(r) for r in state.get("results", [])]
    return sorted(parsed, key=lambda r: order.get(r.agent, len(order)))


# --- entry ----------------------------------------------------------------------------------------


def begin_turn(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    """Clear everything left over from the previous question in this thread. Memory (`turns`) is kept."""
    _emit(runtime, status_event("understanding", "Understanding your question…"))
    return per_turn_reset()


async def classify(
    state: SupervisorState, config: RunnableConfig, runtime: Runtime[SupervisorRuntime]
) -> dict[str, Any]:
    """Rules → follow-up from memory → router LLM. The first confident answer wins; rules cost no tokens."""
    ctx = runtime.context
    question = state["question"]
    rules = classify_by_rules(question)
    window = ctx.settings.AI_MEMORY_WINDOW_TURNS
    follow_up = resolve_follow_up(question, rules, (state.get("turns") or [])[-window:])

    update: dict[str, Any] = {}
    if follow_up is not None:
        match, effective = follow_up.match, follow_up.question
    else:
        outcome = await route_question(question, llm=ctx.llm, config=config, rules=rules)
        match, effective = outcome.match, question
        if outcome.llm_calls:
            update["llm_calls"] = outcome.llm_calls
        if outcome.errors:
            update["errors"] = outcome.errors
    return {
        **update,
        "effective_question": effective,
        "intents": [intent.value for intent in match.intents],
        "appliance": match.appliance.value if match.appliance else None,
        "classification_method": match.method.value,
    }


def plan(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    built = build_agent_plan(
        question=state["effective_question"],
        intents=[Intent(i) for i in state["intents"]],
        appliance=state.get("appliance"),
        deadlines=runtime.context.deadlines(),
    )
    return {"plan": built.model_dump(mode="json")}


def dispatch(state: SupervisorState) -> list[Send] | str:
    """Fan out one `Send` per planned agent. Each agent receives only its own call."""
    built = Plan.model_validate(state["plan"])
    if built.refusal or not built.calls:
        return REFUSE
    return [Send(call.agent.value, {"call": call.model_dump(mode="json")}) for call in built.calls]


# --- agents ---------------------------------------------------------------------------------------


def make_agent_node(name: AgentName):
    """
    A node that runs one specialist inside a failure boundary.

    A timeout or a crash becomes a typed result with no facts, so sibling agents still finish and the
    synthesizer decides what to say. Cancelling on the deadline also frees the connection the agent held.
    """

    async def run_agent(
        payload: AgentInput, config: RunnableConfig, runtime: Runtime[SupervisorRuntime]
    ) -> dict[str, Any]:
        call = AgentCall.model_validate(payload["call"])
        _emit(runtime, status_event("agent_started", _STAGE_MESSAGES[name], agent=name.value))
        agent = runtime.context.agents.get(name)
        started = time.perf_counter()
        try:
            if agent is None:
                raise LookupError(f"no agent registered for {name.value}")
            result = await asyncio.wait_for(agent.run(call, config=config), call.deadline_seconds)
        except TimeoutError:
            logger.warning("agent %s exceeded its %.1fs deadline", name.value, call.deadline_seconds)
            result = AgentResult.failed(name, AgentStatus.TIMEOUT)
        except Exception:
            logger.exception("agent %s failed", name.value)
            result = AgentResult.failed(name, AgentStatus.ERROR)
        elapsed = time.perf_counter() - started
        result = result.model_copy(update={"latency_ms": int(elapsed * 1000)})
        AGENT_RUNS.labels(agent=name.value, status=result.status.value).inc()
        AGENT_SECONDS.labels(agent=name.value).observe(elapsed)
        return {"results": [result.model_dump(mode="json")]}

    run_agent.__name__ = f"run_{name.value}"
    return run_agent


def join(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    """Barrier: runs once, after every dispatched agent has returned (`defer=True`)."""
    _emit(runtime, status_event("synthesizing", "Putting the answer together…"))
    return {}


# --- synthesis ------------------------------------------------------------------------------------


def _answer_update(parts: AnswerParts, **extra: Any) -> dict[str, Any]:
    return {"answer": parts.model_dump(), "llm_used": False, **extra}


async def synthesize(
    state: SupervisorState, config: RunnableConfig, runtime: Runtime[SupervisorRuntime]
) -> dict[str, Any]:
    """
    Turn agent results into one answer.

    * nothing usable came back          → a plain "could not load" answer
    * every result stands on its draft  → merge the drafts, no LLM (refusals, gaps, no data, actions)
    * otherwise                         → one LLM call over the merged facts, then the output gate
    """
    ctx = runtime.context
    good = usable(_results(state))
    if not good:
        return _answer_update(
            unavailable_answer(), fallback_reason="agents_unavailable", route=FINALIZE
        )
    if all(r.answers_itself for r in good):
        return _answer_update(
            merge_drafts(good), fallback_reason=good[0].fallback_reason, route=FINALIZE
        )

    attempts = state.get("attempts", 0)
    facts, analytics, freshness = prompt_payload(
        good,
        max_facts_chars=ctx.settings.AI_MAX_FACTS_CHARS,
        threshold_seconds=ctx.settings.AI_STALE_DATA_SECONDS,
    )
    messages = answer_messages(
        question=state["effective_question"],
        intents=state["intents"],
        facts=facts,
        analytics=analytics,
        freshness=freshness,
        ungrounded_numbers=state.get("ungrounded") or (),
    )
    try:
        result = await ctx.llm.ainvoke(LLMRole.ANSWER, AnswerDraft, messages, config=config)
    except (LLMInvocationError, LLMNotConfiguredError) as exc:
        logger.warning("answer LLM failed, using deterministic answer: %s", exc)
        return _answer_update(
            merge_drafts(good, reason=FallbackReason.LLM_ERROR),
            fallback_reason=FallbackReason.LLM_ERROR.value,
            errors=[str(exc)],
            route=FINALIZE,
        )
    return {
        "answer": result.output.model_dump(),
        "llm_used": True,
        "llm_calls": [llm_record(LLMRole.ANSWER, result)],
        "attempts": attempts + 1,
        "route": OUTPUT_GATE,
    }


def route_after_synthesize(state: SupervisorState) -> str:
    return state.get("route") or FINALIZE


# --- output gate ----------------------------------------------------------------------------------


def output_gate(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    """
    Every number in a model-written answer must trace to the facts it was shown.

    On failure the model gets one chance to correct itself, told which numbers were unsupported. If that
    also fails, the deterministic answer replaces it. `observe` mode records the finding and keeps the
    answer, so a new model or prompt can be measured before it is enforced.
    """
    ctx = runtime.context
    mode = ctx.settings.AI_GROUNDING_MODE
    if mode == "off":
        GROUNDING_CHECKS.labels(result="skipped").inc()
        return {"route": FINALIZE}

    shown = prompt_payload(
        usable(_results(state)),
        max_facts_chars=ctx.settings.AI_MAX_FACTS_CHARS,
        threshold_seconds=ctx.settings.AI_STALE_DATA_SECONDS,
    )
    attempts = state.get("attempts", 1)
    result = check_grounding(state.get("answer") or {}, facts=shown, question=state["effective_question"])
    record = {
        "checked": result.checked,
        "ungrounded": list(result.ungrounded),
        "mode": mode,
        "attempts": attempts,
    }
    if result.ok:
        GROUNDING_CHECKS.labels(result="grounded").inc()
        if attempts > 1:
            SUPERVISOR_REGENERATIONS.labels(outcome="recovered").inc()
        return {"grounding": record, "route": FINALIZE}

    GROUNDING_CHECKS.labels(result="ungrounded").inc()
    logger.warning(
        "answer has numbers not in the facts request_id=%s mode=%s attempt=%d numbers=%s",
        state.get("request_id"),
        mode,
        attempts,
        ",".join(result.ungrounded),
    )
    if mode == "observe":
        return {"grounding": record, "route": FINALIZE}
    if attempts < ctx.settings.AI_ANSWER_MAX_ATTEMPTS:
        return {"grounding": record, "ungrounded": list(result.ungrounded), "route": SYNTHESIZE}
    return {"grounding": record, "route": FALLBACK}


def route_after_gate(state: SupervisorState) -> str:
    return state.get("route") or FINALIZE


def deterministic_fallback(state: SupervisorState) -> dict[str, Any]:
    """The model's answer was not grounded even after a correction: state only what the agents verified."""
    if state.get("attempts", 1) > 1:
        SUPERVISOR_REGENERATIONS.labels(outcome="fell_back").inc()
    parts = merge_drafts(usable(_results(state)), reason=FallbackReason.UNGROUNDED)
    return _answer_update(parts, fallback_reason=FallbackReason.UNGROUNDED.value)


def refuse(state: SupervisorState) -> dict[str, Any]:
    """An off-topic question: no agent runs and no tokens are spent."""
    draft = compose_fallback_answer(
        reason=FallbackReason.OUT_OF_SCOPE,
        intents=[Intent.OUT_OF_SCOPE],
        context=AIContext(),
        analytics=AnalyticsBundle(),
    )
    return _answer_update(
        AnswerParts.model_validate(draft.model_dump()),
        fallback_reason=FallbackReason.OUT_OF_SCOPE.value,
    )


# --- exit -----------------------------------------------------------------------------------------


def finalize(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    """Guardrails the model cannot override, then remember the question for follow-ups."""
    settings = runtime.context.settings
    results = _results(state)
    good = usable(results)
    freshness = merge_freshness(good, threshold_seconds=settings.AI_STALE_DATA_SECONDS)
    answer = finalize_answer(
        state.get("answer") or {},
        freshness=freshness,
        live_advice=any(r.live_advice for r in good),
        passages=all_citations(good),
    )

    failed = [r.agent.value for r in results if not r.status.usable]
    warnings: list[str] = []
    for result in good:
        warnings.extend(result.warnings)
    if failed:
        warnings.append(f"Some information could not be loaded: {', '.join(failed)}.")

    if state.get("fallback_reason") == FallbackReason.OUT_OF_SCOPE.value:
        confidence = "high"
    else:
        confidence = combine_confidence(good, degraded=bool(failed))

    turn = Turn(
        question=state["question"],
        intents=state.get("intents", []),
        appliance=state.get("appliance"),
        agents=[r.agent.value for r in results],
    )
    return {"answer": answer, "confidence": confidence, "warnings": warnings, "turns": [turn]}
