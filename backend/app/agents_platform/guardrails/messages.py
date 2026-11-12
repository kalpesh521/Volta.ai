"""
Deterministic wording for requests the guardrails stop. No model writes these, so an attacker cannot talk the
assistant into a different reply, and they cost no tokens.
"""
from __future__ import annotations

from enum import StrEnum

from app.agents_platform.contracts import AnswerParts


class Refusal(StrEnum):
    """Values of `meta.fallback_reason` for a request a guardrail refused (the out-of-scope refusal is separate)."""

    INPUT_BLOCKED = "input_blocked"
    SENSITIVE_DATA = "sensitive_data"
    INPUT_UNVERIFIED = "input_unverified"
    FEATURE_DISABLED = "feature_disabled"


_PARTS: dict[Refusal, AnswerParts] = {
    Refusal.INPUT_BLOCKED: AnswerParts(
        observation="I can't act on that message.",
        explanation="It asks me to ignore my rules or change how I work, which I don't do.",
        recommendation="Ask a question about your home's solar, battery, grid or appliances instead.",
        estimated_impact="None. Nothing was changed.",
    ),
    Refusal.SENSITIVE_DATA: AnswerParts(
        observation="Your message contains payment details, a bank account or a password, so I did not process it.",
        explanation="I never need those, and they must not be shared in a chat. I did not keep them.",
        recommendation=(
            "Ask again without them. If you shared a real card, password or key, change it now. "
            "A consumer or meter number is fine to include."
        ),
        estimated_impact="None. Nothing was changed.",
    ),
    Refusal.INPUT_UNVERIFIED: AnswerParts(
        observation="I could not confirm that request is safe to carry out, so I did not act on it.",
        explanation="It resembles an attempt to steer the assistant, and I only act on clear, plain requests.",
        recommendation="Rephrase it as a plain question or request about your home's energy.",
        estimated_impact="None. Nothing was changed.",
    ),
    Refusal.FEATURE_DISABLED: AnswerParts(
        observation="That part of the assistant is temporarily switched off.",
        explanation="It was paused by the Suryaa team and nothing is wrong with your home or your request.",
        recommendation="Please try again later.",
        estimated_impact="None. Nothing was changed.",
    ),
}


def refusal_parts(reason: Refusal) -> AnswerParts:
    return _PARTS[reason]
