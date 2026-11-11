"""
Guardrails applied to every answer after it is composed, whichever path produced it.

Shared by the v1 assistant graph and the v2 supervisor so both stamp the data time, flag stale
data and cite documents the same way.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.modules.assistant.domain.answers import FallbackReason
from app.modules.assistant.domain.freshness import DataFreshness, FreshnessStatus

STALE_PREFIX = "Because this data is not current, confirm the live readings before acting. "

_CONFIDENT_REASONS = frozenset(
    {
        FallbackReason.DEVICE_CONTROL.value,
        FallbackReason.OUT_OF_SCOPE.value,
        FallbackReason.KNOWLEDGE_GAP.value,
    }
)
_DATA_ISSUE_CODES = frozenset({"data_quality_degraded", "energy_balance_mismatch"})


def finalize_answer(
    answer: Mapping[str, Any],
    *,
    freshness: DataFreshness,
    live_advice: bool,
    passages: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Stamp the data time, prefix stale-data caution and append document sources."""
    result = dict(answer)
    if freshness.status is FreshnessStatus.STALE and live_advice:
        recommendation = result.get("recommendation", "")
        if not recommendation.startswith(STALE_PREFIX):
            result["recommendation"] = STALE_PREFIX + recommendation
    result["data_time"] = freshness.data_time.isoformat() if freshness.data_time else "unavailable"
    _append_citations(result, passages)
    return result


def _append_citations(answer: dict[str, Any], passages: Sequence[Mapping[str, Any]]) -> None:
    if not passages:
        return
    labels = []
    for passage in passages[:3]:
        page = passage.get("page")
        title = passage.get("title") or "document"
        labels.append(f"{title} p.{page}" if page else title)
    explanation = answer.get("explanation") or ""
    if "Sources:" not in explanation:
        answer["explanation"] = explanation.rstrip() + " Sources: " + "; ".join(labels) + "."


def assess_confidence(
    *,
    fallback_reason: str | None,
    freshness: DataFreshness,
    requires_telemetry: bool,
    live_advice: bool,
    anomalies: Sequence[Mapping[str, Any]] = (),
    tool_statuses: Sequence[str] = (),
) -> str:
    """`high`, `medium` or `low`, from data freshness, data-quality anomalies and failed tools."""
    if fallback_reason in _CONFIDENT_REASONS:
        return "high"
    if freshness.status is not FreshnessStatus.FRESH:
        if requires_telemetry:
            return "low"
        # Setup answers come from onboarding; only the live part is uncertain.
        if live_advice:
            return "medium"
    data_issue = any(a.get("code") in _DATA_ISSUE_CODES for a in anomalies)
    tool_issue = any(status in ("timeout", "error") for status in tool_statuses)
    return "medium" if data_issue or tool_issue else "high"
