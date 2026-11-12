"""
Fast-path injection score: pure, microseconds, no model.

Each category contributes at most once, and the contributions add up to a score in [0, 1]. A single strong
phrase ("ignore all previous instructions") is enough to block. Two weak signals together can reach the grey
zone, which is where the optional classifier decides.

The patterns are deliberately narrow. A false positive refuses a real customer, so a phrase has to look like
an attack, not merely contain a word: "ignore the safety margin" is a battery question and must pass.
English only for now (the multilingual work is in doc 11 §2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    PROMPT_EXTRACTION = "prompt_extraction"
    JAILBREAK_TERM = "jailbreak_term"
    ROLE_HIJACK = "role_hijack"
    TEMPLATE_TOKEN = "template_token"
    ENCODED_PAYLOAD = "encoded_payload"
    HIDDEN_TEXT = "hidden_text"


@dataclass(frozen=True)
class Signal:
    category: Category
    weight: float


@dataclass(frozen=True)
class InjectionScore:
    score: float
    signals: tuple[Signal, ...]

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(s.category.value for s in self.signals)


_I = re.IGNORECASE

# Words that may sit between the verb and the noun of "ignore <all the previous> instructions".
_QUALIFIER = r"(?:all|any|every|the|your|my|these|those|previous|prior|above|earlier|preceding|system|safety|original|initial|developer)"
_STRONG_QUALIFIER = r"(?:all|any|previous|prior|above|earlier|preceding|system|safety|original|initial|developer)"
_NOUN = r"(?:instructions?|rules|guidelines|directives?|restrictions|guardrails|prompts?|programming)"
_VERB = r"(?:ignore|disregard|forget|override|bypass)"

# A weak phrase ("ignore the rules", "what are your instructions") is a hint, not a verdict: on its own it
# stays below the default grey threshold, and it only matters next to another signal.
WEAK_WEIGHT = 0.35

# (category, weight, pattern). The first matching pattern of a category sets that category's weight.
_PATTERNS: tuple[tuple[Category, float, re.Pattern[str]], ...] = (
    (
        Category.INSTRUCTION_OVERRIDE,
        0.9,
        re.compile(rf"\b{_VERB}\s+(?:{_QUALIFIER}\s+){{0,5}}?{_STRONG_QUALIFIER}\s+(?:{_QUALIFIER}\s+){{0,3}}?{_NOUN}\b", _I),
    ),
    (
        Category.INSTRUCTION_OVERRIDE,
        WEAK_WEIGHT,
        re.compile(rf"\b{_VERB}\s+(?:{_QUALIFIER}\s+){{0,5}}{_NOUN}\b", _I),
    ),
    (
        Category.PROMPT_EXTRACTION,
        0.9,
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|display|leak|dump|share|tell)\b[^.\n]{0,20}?"
            r"\b(?:your|the)\s+(?:system|hidden|initial|original|secret|internal|full)\s+"
            r"(?:prompt|instructions?|message|rules)\b",
            _I,
        ),
    ),
    (
        Category.PROMPT_EXTRACTION,
        WEAK_WEIGHT,
        re.compile(
            r"\bsystem\s+prompt\b|\bwhat\s+(?:is|are|were)\s+your\s+(?:system\s+|initial\s+|original\s+)?"
            r"(?:prompt|instructions)\b",
            _I,
        ),
    ),
    (
        Category.JAILBREAK_TERM,
        0.9,
        re.compile(
            r"\b(?:jailbreak(?:ing|ed)?|dan\s+mode|developer\s+mode|do\s+anything\s+now|god\s+mode|unrestricted\s+mode)\b",
            _I,
        ),
    ),
    (
        Category.ROLE_HIJACK,
        0.5,
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on,?\s+you|pretend\s+(?:to\s+be|that\s+you|you\s+are)|"
            r"role-?play\s+as|simulate\s+(?:being|a)|new\s+persona|you\s+have\s+no\s+(?:rules|restrictions|limits))\b",
            _I,
        ),
    ),
    (
        Category.TEMPLATE_TOKEN,
        0.6,
        re.compile(
            r"<\|[a-z_]+\|>|\[/?INST\]|<</?SYS>>|^\s*(?:system|assistant)\s*:|###\s*(?:system|instruction|human|assistant)|"
            r"</?(?:system|instructions?|facts|analytics|data_freshness|document)\b[^>]*>",
            _I | re.MULTILINE,
        ),
    ),
    (
        Category.ENCODED_PAYLOAD,
        0.4,
        re.compile(
            r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])|(?:\\x[0-9a-f]{2}){6,}|(?:%[0-9a-f]{2}){8,}|(?:&#\d{2,5};){4,}",
            _I,
        ),
    ),
)

HIDDEN_TEXT_WEIGHT = 0.3


def score_text(text: str, *, hidden_chars: int = 0) -> InjectionScore:
    """Score normalised text. `hidden_chars` is how many invisible characters normalisation removed."""
    best: dict[Category, float] = {}
    for category, weight, pattern in _PATTERNS:
        if weight > best.get(category, 0.0) and pattern.search(text):
            best[category] = weight
    if hidden_chars > 0:
        best[Category.HIDDEN_TEXT] = HIDDEN_TEXT_WEIGHT
    signals = tuple(Signal(category, weight) for category, weight in best.items())
    return InjectionScore(score=min(1.0, sum(s.weight for s in signals)), signals=signals)
