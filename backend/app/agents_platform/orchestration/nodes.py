"""
Supervisor graph nodes.

    begin_turn → input_guard ─┬→ classify → plan ─┬→ <agent> ×N (parallel, via Send) → join → synthesize → output_gate → finalize
                                  ├→ <device_action> (exclusive)  ───────┘                     ↑      │
                                  └→ refuse ←── (blocked input, off-topic) ────────────────────┼──────┤
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
from app.agents_platform.guardrails.classifier import classify_injection
from app.agents_platform.guardrails.input_guard import Decision
from app.agents_platform.guardrails.messages import Refusal, refusal_parts
from app.agents_platform.guardrails.output_guard import check_answer
from app.agents_platform.guardrails.pii.masker import redact_leaks
from app.agents_platform.orchestration.events import status_event
from app.agents_platform.orchestration.explain_and_do import offer_text, split_explain_and_do
from app.agents_platform.orchestration.memory import resolve_follow_up
from app.agents_platform.orchestration.planner import build_agent_plan
from app.agents_platform.orchestration.planning_intents import (
    AnyIntent,
    detect_planning_intents,
    merge_intents,
    parse_intent,
)
from app.agents_platform.orchestration.retry import max_attempts, run_with_retry
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
from app.agents_platform.registry.flags import agent_flag
from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.gated import GatedLLM
from app.core.metrics import (
    AGENT_RUNS,
    AGENT_SECONDS,
    GROUNDING_CHECKS,
    GUARD_DECISIONS,
    KILL_SWITCH_HITS,
    OUTPUT_VIOLATIONS,
    PII_ENTITIES,
    SUPERVISOR_REGENERATIONS,
)
from app.modules.assistant.domain.answers import AnswerDraft, FallbackReason, compose_fallback_answer
from app.modules.assistant.domain.analytics import AnalyticsBundle
from app.modules.assistant.domain.context import AIContext
from app.modules.assistant.domain.finalize import finalize_answer
from app.modules.assistant.domain.grounding import check_grounding
from app.modules.assistant.domain.intents import Intent, classify_by_rules
from app.modules.assistant.graph.routing import route_question
from app.modules.assistant.graph.state import llm_record

logger = logging.getLogger("volta.agents.supervisor")

BEGIN_TURN = "begin_turn"
INPUT_GUARD = "input_guard"
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
    AgentName.FORECAST: "Forecasting your solar and usage…",
    AgentName.OPTIMIZER: "Working out the best schedule…",
    AgentName.BILL_TARIFF: "Calculating your bill and tariff…",
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


def _untrusted(state: SupervisorState) -> bool:
    return bool((state.get("guard") or {}).get("untrusted"))


async def input_guard(
    state: SupervisorState, config: RunnableConfig, runtime: Runtime[SupervisorRuntime]
) -> dict[str, Any]:
    """
    Gate G1, slow path. The fast path already ran before the graph (normalise, score, mask); this node acts on
    its verdict.

    * block  → refuse; no agent runs and no model is called
    * review → a grey-zone input. With the classifier on, a small model decides: injection refuses, clean lifts
               the restrictions. Without it, or when it cannot decide, the input stays *untrusted*: no router model
               and no device action, only plain facts.
    * allow  → continue
    """
    guard = dict(state.get("guard") or {})
    decision = guard.get("decision", Decision.ALLOW.value)
    update: dict[str, Any] = {}

    if decision == Decision.REVIEW.value and runtime.context.settings.AI_GUARD_CLASSIFIER_ENABLED:
        outcome = await classify_injection(runtime.context.llm, state["question"], config=config)
        if outcome.llm_calls:
            update["llm_calls"] = outcome.llm_calls
        if outcome.injection is True:
            GUARD_DECISIONS.labels(stage="classifier", decision="block").inc()
            guard.update(decision=Decision.BLOCK.value, reason=Refusal.INPUT_BLOCKED.value, untrusted=False)
            decision = Decision.BLOCK.value
        elif outcome.injection is False:
            GUARD_DECISIONS.labels(stage="classifier", decision="allow").inc()
            guard.update(decision=Decision.ALLOW.value, reason=None, untrusted=False)
            decision = Decision.ALLOW.value
        else:
            GUARD_DECISIONS.labels(stage="classifier", decision="review").inc()
            if outcome.error:
                update["errors"] = [outcome.error]

    if decision == Decision.BLOCK.value:
        reason = guard.get("reason") or Refusal.INPUT_BLOCKED.value
        update["plan"] = Plan(refusal=reason).model_dump(mode="json")
    return {**update, "guard": guard}


def route_after_input_guard(state: SupervisorState) -> str:
    return REFUSE if (state.get("plan") or {}).get("refusal") else CLASSIFY


async def classify(
    state: SupervisorState, config: RunnableConfig, runtime: Runtime[SupervisorRuntime]
) -> dict[str, Any]:
    """Rules → follow-up from memory → router LLM. The first confident answer wins; rules cost no tokens."""
    ctx = runtime.context
    # An untrusted input is never shown to the router model: rules and memory only.
    router = GatedLLM(ctx.llm, reason="untrusted input") if _untrusted(state) else ctx.llm
    question = state["question"]
    rules = classify_by_rules(question)
    window = ctx.settings.AI_MEMORY_WINDOW_TURNS
    follow_up = resolve_follow_up(question, rules, (state.get("turns") or [])[-window:])

    update: dict[str, Any] = {}
    combined = (
        split_explain_and_do(question)
        if follow_up is None and rules.primary is Intent.DEVICE_CONTROL and not _untrusted(state)
        else None
    )
    if combined is not None:
        # A question and a command in one message: answer the question, offer the command (never run it here).
        match, effective = combined.match, combined.question
        update["offered_command"] = combined.command
    elif follow_up is not None:
        match, effective = follow_up.match, follow_up.question
    else:
        earlier = [t["question"] for t in (state.get("turns") or [])[-window:]]
        outcome = await route_question(question, llm=router, config=config, rules=rules, history=earlier)
        match, effective = outcome.match, question
        if outcome.llm_calls:
            update["llm_calls"] = outcome.llm_calls
        if outcome.errors:
            update["errors"] = outcome.errors
    intents: tuple[AnyIntent, ...] = match.intents
    if follow_up is None and combined is None:
        # A planning question (forecast, optimise, bill what-if) is recognised by rules, beside the legacy routing.
        planning = detect_planning_intents(question, match)
        if planning:
            intents = merge_intents(question, planning, match.intents)
    return {
        **update,
        "effective_question": effective,
        "intents": [intent.value for intent in intents],
        "appliance": match.appliance.value if match.appliance else None,
        "classification_method": match.method.value,
    }


def plan(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
    intents = [parse_intent(i) for i in state["intents"]]
    if _untrusted(state) and intents and intents[0] is Intent.DEVICE_CONTROL:
        # Grey-zone input plus a device command: never act on it (doc 04 §2 decision table).
        return {"plan": Plan(refusal=Refusal.INPUT_UNVERIFIED.value).model_dump(mode="json")}
    built = build_agent_plan(
        question=state["effective_question"],
        intents=intents,
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
        if not runtime.context.flags.agent_enabled(name):
            KILL_SWITCH_HITS.labels(flag=agent_flag(name)).inc()
            AGENT_RUNS.labels(agent=name.value, status=AgentStatus.DISABLED.value).inc()
            return {"results": [AgentResult.failed(name, AgentStatus.DISABLED).model_dump(mode="json")]}
        if not runtime.context.entitlements.allows(name):
            AGENT_RUNS.labels(agent=name.value, status=AgentStatus.REFUSED.value).inc()
            return {"results": [_plan_required(name).model_dump(mode="json")]}
        _emit(runtime, status_event("agent_started", _STAGE_MESSAGES[name], agent=name.value))
        agent = runtime.context.agents.get(name)
        started = time.perf_counter()
        try:
            if agent is None:
                raise LookupError(f"no agent registered for {name.value}")
            settings = runtime.context.settings
            result = await asyncio.wait_for(
                run_with_retry(
                    lambda: agent.run(call, config=config),
                    agent=name,
                    attempts=max_attempts(name, settings),
                    backoff_seconds=settings.AI_AGENT_RETRY_BACKOFF_SECONDS,
                ),
                call.deadline_seconds,
            )
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


def _plan_required(name: AgentName) -> AgentResult:
    """A paid feature on a free plan: refused with fixed wording, no data read and no model asked."""
    return AgentResult(
        agent=name,
        status=AgentStatus.REFUSED,
        draft=refusal_parts(Refusal.PLAN_REQUIRED),
        fallback_reason=Refusal.PLAN_REQUIRED.value,
        warnings=["Part of this question needs the Plus plan."],
        confidence="high",
        live_advice=False,
    )


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

    * every agent is switched off       → "temporarily switched off", no data sources were asked
    * nothing usable came back          → a plain "could not load" answer
    * every result stands on its draft  → merge the drafts, no LLM (refusals, gaps, no data, actions)
    * the model is off for this request → merge the drafts (kill switch, spent budget, no API key)
    * otherwise                         → one LLM call over the merged facts, then the output gate
    """
    ctx = runtime.context
    results = _results(state)
    good = usable(results)
    if not good:
        if results and all(r.status is AgentStatus.DISABLED for r in results):
            return _answer_update(
                refusal_parts(Refusal.FEATURE_DISABLED),
                fallback_reason=Refusal.FEATURE_DISABLED.value,
                route=FINALIZE,
            )
        return _answer_update(
            unavailable_answer(), fallback_reason="agents_unavailable", route=FINALIZE
        )
    if all(r.answers_itself for r in good):
        return _answer_update(
            merge_drafts(good), fallback_reason=good[0].fallback_reason, route=FINALIZE
        )

    if not ctx.llm.is_available(LLMRole.ANSWER):
        return _answer_update(
            merge_drafts(good, reason=FallbackReason.LLM_UNAVAILABLE),
            fallback_reason=FallbackReason.LLM_UNAVAILABLE.value,
            route=FINALIZE,
        )

    attempts = state.get("attempts", 0)
    facts, analytics, freshness = prompt_payload(
        good,
        max_facts_chars=ctx.settings.AI_MAX_FACTS_CHARS,
        threshold_seconds=ctx.settings.AI_STALE_DATA_SECONDS,
    )
    messages = ctx.prompts.answer_messages(
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
    Gate G8 for a model-written answer: what it may not claim, then whether its numbers are real.

    1. Claims and advice a model must never make ("I switched it off", "bypass the breaker"). Not regenerated:
       the verified deterministic answer replaces it at once.
    2. Numbers: every one must trace to the facts the model was shown (`_grounding_gate`).
    """
    mode = runtime.context.settings.AI_OUTPUT_POLICY_MODE
    if mode == "off":
        return _grounding_gate(state, runtime)

    violations = check_answer(state.get("answer") or {})
    if violations:
        for violation in violations:
            OUTPUT_VIOLATIONS.labels(kind=violation.kind.value).inc()
        logger.warning(
            "answer withheld by output policy request_id=%s mode=%s rules=%s",
            state.get("request_id"),
            mode,
            ",".join(v.rule for v in violations),
        )
        if mode == "enforce":
            return {"output_violations": [v.rule for v in violations], "route": FALLBACK}
    return _grounding_gate(state, runtime)


def _grounding_gate(state: SupervisorState, runtime: Runtime[SupervisorRuntime]) -> dict[str, Any]:
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
    """The model's answer was withheld (a safety rule) or not grounded even after a correction: state only what the agents verified."""
    if state.get("output_violations"):
        reason = FallbackReason.POLICY
    else:
        reason = FallbackReason.UNGROUNDED
        if state.get("attempts", 1) > 1:
            SUPERVISOR_REGENERATIONS.labels(outcome="fell_back").inc()
    return _answer_update(merge_drafts(usable(_results(state)), reason=reason), fallback_reason=reason.value)


def refuse(state: SupervisorState) -> dict[str, Any]:
    """A request that must not be answered: no agent runs and no tokens are spent. The wording is fixed."""
    reason = (state.get("plan") or {}).get("refusal")
    if reason in {r.value for r in Refusal}:
        return _answer_update(refusal_parts(Refusal(reason)), fallback_reason=reason)
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


_DETERMINISTIC_REFUSALS = frozenset({FallbackReason.OUT_OF_SCOPE.value, *(r.value for r in Refusal)})
_REDACTED_FIELDS = ("observation", "explanation", "recommendation", "estimated_impact")


def _redact_answer(answer: dict[str, str]) -> dict[str, str]:
    """Gate G8, personal data: nothing the user must not be shown leaves in the reply, whatever put it there."""
    cleaned = dict(answer)
    for name in _REDACTED_FIELDS:
        if name in cleaned:
            cleaned[name], found = redact_leaks(cleaned[name])
            for cls, count in found.items():
                PII_ENTITIES.labels(pii_class=cls, where="output").inc(count)
    return cleaned


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

    command = state.get("offered_command")
    if command and state.get("fallback_reason") not in _DETERMINISTIC_REFUSALS:
        answer["recommendation"] = f"{answer.get('recommendation', '')} {offer_text(command)}".strip()
    answer = _redact_answer(answer)

    failed = [r.agent.value for r in results if not r.status.usable and r.status is not AgentStatus.DISABLED]
    switched_off = [r.agent.value for r in results if r.status is AgentStatus.DISABLED]
    warnings: list[str] = []
    for result in good:
        warnings.extend(result.warnings)
    if failed:
        warnings.append(f"Some information could not be loaded: {', '.join(failed)}.")
    if switched_off and good:
        warnings.append(f"Some features are temporarily switched off: {', '.join(switched_off)}.")

    if (state.get("guard") or {}).get("untrusted"):
        warnings.append("Your message looked unusual, so I only used plain facts about your home.")

    if state.get("fallback_reason") in _DETERMINISTIC_REFUSALS:
        confidence = "high"
    else:
        # An answer that is missing an agent's part is not fully confident, whether the agent failed or was switched off.
        confidence = combine_confidence(good, degraded=bool(failed or switched_off))

    turn = Turn(
        question=state["question"],
        intents=state.get("intents", []),
        appliance=state.get("appliance"),
        agents=[r.agent.value for r in results],
    )
    return {"answer": answer, "confidence": confidence, "warnings": warnings, "turns": [turn]}
