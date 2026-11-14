"""
Planning intents: forecast, optimise, bill what-if. Detected by rules, for the v2 supervisor only.

They are a separate enum on purpose. The v1 assistant shares `Intent` and its rule table, and its eval set pins
how every question there is routed; adding members to `Intent` would change v1 until v1 is retired. A planning
intent is detected *beside* the legacy classification and takes the primary place when it matches, so a question
that mixes both ("will I have enough solar tomorrow, and how is the battery now?") runs both kinds of agent.

Deterministic first (doc 01 principle 2): no model is asked. The patterns are deliberately narrow, in the same
spirit as the injection patterns: a miss falls back to the legacy answer, which is correct for everything that
is not clearly a planning question.

What is never taken over: a request to act on a device (`device_control`), the single-appliance "can I run it now"
verdict, a full overview, and document questions. Those have their own verified paths.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from app.modules.assistant.domain.intents import MAX_INTENTS, Intent, IntentMatch


class PlanningIntent(StrEnum):
    FORECAST = "forecast"
    OPTIMIZE = "optimize"
    BILL_ESTIMATE = "bill_estimate"


AnyIntent = Intent | PlanningIntent

# Legacy primaries whose own verified path must not be taken over by a planning agent: acting on a device, and a
# full overview. "Can I run the geyser now?" (a timing question that names an appliance) is also left alone: it has
# a deterministic single-appliance verdict. The same intent without an appliance ("how should I use my battery
# tomorrow?") is a planning question. `document` is not protected: the legacy rules treat any "bill" as a
# document question, and the planning patterns below are narrow enough not to need that shield.
_PROTECTED = frozenset({Intent.DEVICE_CONTROL, Intent.FULL_OVERVIEW})

_ENERGY_TERM = r"(?:solar|generat\w*|produc\w*|consum\w*|usage|load|surplus|battery|grid|import\w*|export\w*|energy|power|electricity)"
_PREDICTION_CUE = r"(?:how\s+much|how\s+many|will|going\s+to|expect\w*|enough|likely|estimate\w*|what\s+to\s+expect)"
_FUTURE_TERM = (
    r"(?:tomorrow|tonight|next\s+(?:\d+\s+)?(?:hours?|day|days)|next\s+few\s+hours|rest\s+of\s+(?:the\s+day|today)|"
    r"this\s+(?:afternoon|evening|morning)|later\s+today|by\s+(?:evening|night|morning)|over\s+the\s+next|coming\s+(?:hours|days))"
)


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


_RULES: dict[PlanningIntent, tuple[re.Pattern[str], ...]] = {
    PlanningIntent.FORECAST: _rx(
        rf"\b(?:forecast\w*|predict\w*|projection|projected|outlook)\b.*\b{_ENERGY_TERM}\b",
        rf"\b{_ENERGY_TERM}\b.*\b(?:forecast\w*|predict\w*|projection|projected|outlook)\b",
        # A future word alone is not a forecast ("can I ignore the reserve tonight?" is about now): it needs a cue.
        rf"\b{_PREDICTION_CUE}\b.*\b{_ENERGY_TERM}\b.*\b{_FUTURE_TERM}\b",
        rf"\b{_PREDICTION_CUE}\b.*\b{_FUTURE_TERM}\b.*\b{_ENERGY_TERM}\b",
        rf"\b{_FUTURE_TERM}\b.*\b{_PREDICTION_CUE}\b.*\b{_ENERGY_TERM}\b",
        r"\bhow\s+much\s+(?:solar|power|energy|electricity)\b.*\bwill\b",
        r"\bwill\s+(?:i|we|my\s+\w+)\b.*\b(?:enough|surplus|shortfall|run\s+out|import|export)\b",
    ),
    PlanningIntent.OPTIMIZE: _rx(
        r"\boptimi[sz]\w*\b",
        r"\bplan\s+(?:my|the)\s+(?:day|energy|usage|power|appliances|charging)\b",
        r"\b(?:battery|charging|energy)\s+(?:strategy|plan|schedule)\b",
        r"\bhow\s+should\s+i\s+(?:use|run|charge|manage|plan)\b",
        r"\b(?:cheapest|best|optimal|ideal|smartest)\s+(?:way|plan|schedule|strategy)\b",
        r"\b(?:reduce|cut|lower|minimi[sz]e)\s+(?:my\s+)?(?:grid\s+)?(?:bill|import|cost|spend)\b",
        r"\bmaximi[sz]e\s+(?:my\s+)?(?:savings|self[\s-]?consumption|solar\s+use)\b",
        r"\bshift\w*\s+(?:my\s+)?(?:load|usage|appliances)\b",
        r"\bwhat\s+(?:time|hours?)\s+(?:should|is\s+best|are\s+best)\b.*\b(?:run|charge|use)\b.*\b(?:appliances|loads|ev|everything)\b",
    ),
    PlanningIntent.BILL_ESTIMATE: _rx(
        r"\bwhat\s+if\b.*\b(?:solar|battery|panels?|bill|tariff|usage|consumption|shift|install|add)\b",
        r"\bhow\s+much\s+would\b.*\b(?:bill|pay|save|cost)\b",
        r"\bestimate\w*\b.*\bbill\b",
        r"\b(?:projected|expected|next\s+month'?s?|this\s+month'?s?)\s+(?:electricity\s+)?bill\b",
        r"\bbill\b.*\b(?:next|this)\s+month\b",
        r"\bpay\s*-?\s*back\b",
        r"\b(?:add|install|get|buy)\w*\b.*\b(?:solar|battery|panels?|kwp|kwh)\b.*\b(?:save|bill|worth|payback)\b",
        r"\b(?:tariff|bill)\s+(?:slab|slabs|breakdown|calculation)\b",
        r"\btime[\s-]*of[\s-]*day\b|\btod\s+(?:tariff|rates?)\b",
        r"\b(?:compare|switch(?:ing)?)\b.*\btariff\b",
    ),
}

# Higher first. When several match, the highest-priority one is the primary intent.
_PRIORITY: tuple[PlanningIntent, ...] = (
    PlanningIntent.BILL_ESTIMATE,
    PlanningIntent.OPTIMIZE,
    PlanningIntent.FORECAST,
)


def parse_intent(value: str) -> AnyIntent:
    """An intent as stored in graph state: either kind, by value."""
    try:
        return Intent(value)
    except ValueError:
        return PlanningIntent(value)


def detect_planning_intents(question: str, legacy: IntentMatch) -> tuple[PlanningIntent, ...]:
    """Planning intents in the question, in priority order. Empty when none apply or a protected path owns it."""
    if legacy.primary in _PROTECTED or (legacy.primary is Intent.APPLIANCE_TIMING and legacy.appliance is not None):
        return ()
    text = " ".join(question.lower().split())
    matched = {intent for intent, patterns in _RULES.items() if any(p.search(text) for p in patterns)}
    return tuple(intent for intent in _PRIORITY if intent in matched)


# A second clause: the question asks for more than the planning answer, so the legacy agents still run.
_TWO_PART = re.compile(r"\b(?:and|also|plus|then|as well)\b|;|\?.+\?")


# The legacy rules call any "bill" a document question. Only these words really ask for a document.
_DOCUMENT_WORDS = re.compile(
    r"\b(?:manual|fault\s*codes?|warrant\w*|polic(?:y|ies)|tariff\s+order|circular|certificate|"
    r"uploaded|document\w*|pdf|net\s*-?\s*metering\s+(?:rules?|polic\w*))\b"
)


def merge_intents(question: str, planning: Sequence[PlanningIntent], legacy: Sequence[Intent]) -> tuple[AnyIntent, ...]:
    """
    Planning intents first. The legacy intents the same words also matched ("solar" in "how much solar will I
    generate tomorrow?") are dropped, because the planning agent already covers that topic and a second agent
    would only repeat it. They stay when the question is plainly two-part, and a document intent stays when the question really asks for
    a document: the planning agents do not read documents.
    """
    generic = {Intent.LIVE_OVERVIEW, Intent.OUT_OF_SCOPE}
    two_part = bool(_TWO_PART.search(question.lower()))
    wants_document = bool(_DOCUMENT_WORDS.search(question.lower()))
    extra = [i for i in legacy if i not in generic and (two_part or (i is Intent.DOCUMENT and wants_document))]
    return (*planning, *extra)[:MAX_INTENTS]
