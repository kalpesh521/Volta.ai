"""
Pure helpers the synthesizer and the output gate share: merge agent results, build the one prompt
payload the model sees, and compose deterministic wording when no LLM answer can be used.

No I/O and no graph types here, so every rule is a plain function that is easy to test.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.agents_platform.contracts import AgentResult, AnswerParts
from app.modules.assistant.domain.answers import FallbackReason, fallback_note
from app.modules.assistant.domain.context import fit_to_budget
from app.modules.assistant.domain.freshness import DataFreshness, FreshnessStatus
from app.modules.assistant.domain.projection import project_freshness

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def usable(results: Iterable[AgentResult]) -> list[AgentResult]:
    return [r for r in results if r.status.usable]


def merge_freshness(results: Sequence[AgentResult], *, threshold_seconds: int) -> DataFreshness:
    """
    The oldest real reading among the results. Knowledge never reads telemetry, so its empty
    freshness must not turn an otherwise fresh answer into a "missing" one.
    """
    known = [DataFreshness.model_validate(r.freshness) for r in results if r.freshness]
    with_data = [f for f in known if f.data_time is not None]
    if with_data:
        return min(with_data, key=lambda f: f.data_time)  # type: ignore[arg-type,return-value]
    if known:
        return known[0]
    return DataFreshness(status=FreshnessStatus.MISSING, threshold_seconds=threshold_seconds)


def merge_payloads(results: Sequence[AgentResult]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Facts and analytics from every result. Shared keys (the household profile) carry identical data."""
    facts: dict[str, Any] = {}
    analytics: dict[str, Any] = {}
    for result in results:
        facts.update(result.facts)
        analytics.update(result.analytics)
    return facts, analytics


def prompt_payload(
    results: Sequence[AgentResult], *, max_facts_chars: int, threshold_seconds: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """
    (facts, analytics, freshness) exactly as the answer prompt shows them.

    The output gate calls this too, so it checks the answer's numbers against what the model was
    given, not against something slightly different.
    """
    facts, analytics = merge_payloads(results)
    freshness = merge_freshness(results, threshold_seconds=threshold_seconds)
    fitted, _ = fit_to_budget(facts, max_facts_chars)
    return fitted, analytics, project_freshness(freshness.model_dump(mode="json"))


def combine_confidence(results: Sequence[AgentResult], *, degraded: bool) -> str:
    """The weakest confidence among the results; one failed agent caps it at `medium`."""
    if not results:
        return "low"
    weakest = min((r.confidence for r in results), key=_CONFIDENCE_RANK.__getitem__)
    if degraded and _CONFIDENCE_RANK[weakest] > _CONFIDENCE_RANK["medium"]:
        return "medium"
    return weakest


def _unique(parts: Iterable[str]) -> list[str]:
    return [p for p in dict.fromkeys(part.strip() for part in parts) if p]


def merge_drafts(results: Sequence[AgentResult], *, reason: FallbackReason | None = None) -> AnswerParts:
    """
    One deterministic answer from the agents' own drafts.

    A single draft passes through untouched. With several, each field joins the distinct sentences.
    `reason` adds the sentence explaining why the AI wording was not used, once, unless every draft
    already carries its own reason.
    """
    drafts = [r.draft for r in results if r.draft is not None]
    if not drafts:
        return unavailable_answer()
    fields = ("observation", "explanation", "recommendation", "estimated_impact")
    merged = {name: " ".join(_unique(getattr(d, name) for d in drafts)) for name in fields}
    if reason is not None and any(r.fallback_reason is None for r in results if r.draft is not None):
        note = fallback_note(reason).strip()
        if note:
            merged["explanation"] = f"{merged['explanation']} {note}".strip()
    return AnswerParts(**merged)


def unavailable_answer() -> AnswerParts:
    """When no agent produced a usable result: say so plainly instead of guessing."""
    return AnswerParts(
        observation="I could not load the information needed to answer that right now.",
        explanation="The data sources did not respond in time.",
        recommendation="Please try again in a moment.",
        estimated_impact="Not enough data to estimate.",
    )


def all_citations(results: Sequence[AgentResult]) -> list[dict[str, Any]]:
    return [c for r in results for c in r.citations]
