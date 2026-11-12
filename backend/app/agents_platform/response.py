"""Map the supervisor's final state to the v2 API contract."""
from __future__ import annotations

from typing import Any

from app.agents_platform.api.schemas import AgentRunOut, GuardOut, SupervisorResponse
from app.agents_platform.contracts import AgentResult, Plan
from app.agents_platform.orchestration.state import SupervisorState
from app.agents_platform.orchestration.synthesis import (
    all_citations,
    merge_freshness,
    usable,
)
from app.ai.config import LLMRole
from app.agents_platform.agents.device_action.schemas import ActionResponse
from app.modules.assistant.prompts import PROMPT_VERSION
from app.modules.assistant.schemas import (
    AssistantAnswer,
    AssistantMeta,
    FreshnessOut,
    GroundingOut,
    SourceOut,
    ToolCallOut,
    UsageOut,
)


def results_of(state: SupervisorState) -> list[AgentResult]:
    return [AgentResult.model_validate(r) for r in state.get("results", [])]


def route_of(state: SupervisorState) -> str:
    return Plan.model_validate(state.get("plan") or {}).route


def to_response(
    *,
    household_id: str,
    question: str,
    conversation_id: str,
    state: SupervisorState,
    request_id: str,
    latency_ms: int,
    prompt_version: str = PROMPT_VERSION,
    stale_threshold_seconds: int,
) -> SupervisorResponse:
    results = results_of(state)
    good = usable(results)
    llm_calls: list[dict[str, Any]] = [*state.get("llm_calls", []), *(c for r in results for c in r.llm_calls)]
    usage = UsageOut(
        input_tokens=sum(c["input_tokens"] for c in llm_calls),
        output_tokens=sum(c["output_tokens"] for c in llm_calls),
        total_tokens=sum(c["total_tokens"] for c in llm_calls),
    )
    answer_model = next(
        (c["model"] for c in reversed(llm_calls) if c["role"] == LLMRole.ANSWER.value), None
    )
    llm_used = bool(state.get("llm_used"))
    freshness = merge_freshness(good, threshold_seconds=stale_threshold_seconds)
    tool_calls = [ToolCallOut.model_validate(c) for r in results for c in r.tool_calls]
    proposals = [p for r in results for p in r.proposals]
    grounding = state.get("grounding")
    guard = state.get("guard")

    return SupervisorResponse(
        household_id=household_id,
        question=question,
        conversation_id=conversation_id,
        route=route_of(state),
        answer=AssistantAnswer.model_validate(state["answer"]),
        confidence=state.get("confidence", "low"),  # type: ignore[arg-type]
        warnings=list(dict.fromkeys(state.get("warnings", []))),
        intents=state.get("intents", []),
        appliance=state.get("appliance"),
        tools_used=[c.name for c in tool_calls if c.status == "ok"],
        analytics_used=list(dict.fromkeys(a for r in good for a in r.analytics_used)),
        sources=_sources(all_citations(good)),
        data_freshness=FreshnessOut(
            status=freshness.status.value,  # type: ignore[arg-type]
            data_time=freshness.data_time.isoformat() if freshness.data_time else None,
            age_seconds=freshness.age_seconds,
            threshold_seconds=freshness.threshold_seconds,
        ),
        agents=[
            AgentRunOut(agent=r.agent, status=r.status, latency_ms=r.latency_ms, confidence=r.confidence)
            for r in results
        ],
        guard=GuardOut(
            decision=guard["decision"],
            reason=guard.get("reason"),
            categories=guard.get("categories", []),
            pii_masked=guard.get("pii", {}),
            untrusted=bool(guard.get("untrusted")),
        )
        if guard
        else None,
        proposal=ActionResponse.model_validate(proposals[0]) if proposals else None,
        meta=AssistantMeta(
            request_id=request_id,
            llm_used=llm_used,
            model=answer_model if llm_used else None,
            classification_method=state.get("classification_method", "rules"),
            fallback_reason=None if llm_used else state.get("fallback_reason"),
            prompt_version=prompt_version,
            latency_ms=latency_ms,
            usage=usage,
            tool_calls=tool_calls,
            grounding=GroundingOut.model_validate(
                {k: grounding[k] for k in ("mode", "checked", "ungrounded")}
            )
            if grounding
            else None,
        ),
    )


def _sources(passages: list[dict[str, Any]]) -> list[SourceOut]:
    sources: list[SourceOut] = []
    for passage in passages:
        try:
            sources.append(SourceOut.model_validate(passage))
        except (TypeError, ValueError):
            continue
    return sources
