"""
Gate G1, the fast path: one verdict per question, before any model, agent or checkpoint sees it.

    normalise → score for injection → find personal data → mask

The result carries the text the rest of the system will use. That text is already normalised and masked, so
the checkpoint (which stores the question as the conversation's memory), the logs and the traces never hold
a raw email or a hidden character. A refused question is replaced by a fixed placeholder: attacker text and
pasted secrets are not kept at all.

Modes (`AI_GUARD_MODE`, `AI_PII_MODE`):

    enforce   act on the verdict
    observe   measure only: the verdict is counted and logged, the question passes through unchanged
    off       skip the check

The slow path (an optional model for grey-zone inputs) lives in `classifier.py` and runs inside the graph.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.agents_platform.guardrails.injection import InjectionScore, score_text
from app.agents_platform.guardrails.messages import Refusal
from app.agents_platform.guardrails.normalize import normalize
from app.agents_platform.guardrails.pii import masker
from app.ai.config import AISettings
from app.core.metrics import GUARD_DECISIONS, PII_ENTITIES

logger = logging.getLogger("volta.security")

WITHHELD = "[input withheld]"


class Decision(StrEnum):
    ALLOW = "allow"
    REVIEW = "review"  # grey zone: the classifier decides, or the question is treated as untrusted
    BLOCK = "block"


@dataclass(frozen=True)
class GuardedInput:
    text: str
    decision: Decision = Decision.ALLOW
    reason: Refusal | None = None
    score: float = 0.0
    categories: tuple[str, ...] = ()
    pii_counts: Mapping[str, int] = field(default_factory=dict)
    # token -> original value. Raw personal data: store it in the vault and drop it. Never persisted.
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)

    def to_state(self) -> dict[str, Any]:
        """The part that goes into graph state (and therefore checkpoints). Never contains raw values."""
        return {
            "decision": self.decision.value,
            "reason": self.reason.value if self.reason else None,
            "score": round(self.score, 3),
            "categories": list(self.categories),
            "pii": dict(self.pii_counts),
            "untrusted": self.decision is Decision.REVIEW,
        }


def fingerprint(text: str) -> str:
    """A short stable hash to correlate a refused input across log lines without logging it."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class InputGuard:
    def __init__(self, settings: AISettings) -> None:
        self._guard_mode = settings.AI_GUARD_MODE
        self._pii_mode = settings.AI_PII_MODE
        self._block_score = settings.AI_GUARD_BLOCK_SCORE
        self._grey_score = settings.AI_GUARD_GREY_SCORE

    def inspect(self, question: str, *, request_id: str = "") -> GuardedInput:
        if self._guard_mode == "off" and self._pii_mode == "off":
            return GuardedInput(text=question)

        normalized = normalize(question)
        injection = (
            score_text(normalized.text, hidden_chars=normalized.invisible_removed)
            if self._guard_mode != "off"
            else InjectionScore(0.0, ())
        )
        masking = masker.mask(normalized.text) if self._pii_mode != "off" else None
        counts = dict(masking.counts) if masking else {}
        for cls, n in counts.items():
            PII_ENTITIES.labels(pii_class=cls, where="input").inc(n)

        sensitive = bool(masking and masking.blocked)
        injection_blocks = injection.score >= self._block_score
        reason: Refusal | None = None
        if sensitive and self._pii_mode == "enforce":
            reason = Refusal.SENSITIVE_DATA
        elif injection_blocks and self._guard_mode == "enforce":
            reason = Refusal.INPUT_BLOCKED

        if reason is not None:
            self._record("block", reason, injection, counts, question, request_id)
            return GuardedInput(
                text=WITHHELD,
                decision=Decision.BLOCK,
                reason=reason,
                score=injection.score,
                categories=injection.categories,
                pii_counts=counts,
            )
        if sensitive or injection_blocks:  # observe mode: recorded, not acted on
            self._record("observed_block", Refusal.SENSITIVE_DATA if sensitive else Refusal.INPUT_BLOCKED,
                         injection, counts, question, request_id)

        grey = self._guard_mode == "enforce" and not injection_blocks and injection.score >= self._grey_score
        verdict = Decision.REVIEW if grey else Decision.ALLOW
        GUARD_DECISIONS.labels(stage="fast", decision=verdict.value).inc()

        applied = masking if self._pii_mode == "enforce" else None
        return GuardedInput(
            text=applied.text if applied else normalized.text,
            decision=verdict,
            reason=Refusal.INPUT_UNVERIFIED if verdict is Decision.REVIEW else None,
            score=injection.score,
            categories=injection.categories,
            pii_counts=counts,
            tokens=applied.tokens if applied else {},
        )

    @staticmethod
    def _record(
        outcome: str,
        reason: Refusal,
        injection: InjectionScore,
        counts: Mapping[str, int],
        question: str,
        request_id: str,
    ) -> None:
        GUARD_DECISIONS.labels(stage="fast", decision=outcome).inc()
        # A security event: what happened and why, never the text itself.
        logger.warning(
            "input %s request_id=%s reason=%s score=%.2f categories=%s pii=%s input_sha=%s chars=%d",
            "blocked" if outcome == "block" else "would_block",
            request_id,
            reason.value,
            injection.score,
            ",".join(injection.categories),
            ",".join(sorted(counts)),
            fingerprint(question),
            len(question),
        )
