"""
Follow-up resolution from thread memory.

"Why?", "and tomorrow?", "what about the washing machine?" cannot be routed on their own. When the
rules are not confident and the question reads like a follow-up, it inherits the previous turn's
intents. That costs no tokens and is deterministic. The agents then get the question together
with the one it follows, so their facts and wording are about the right topic.

Only the question text of the earlier turn is carried, never its answer, so a follow-up is always
answered from live data and an old number can never be repeated as if it were current.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.agents_platform.orchestration.planning_intents import parse_intent
from app.agents_platform.orchestration.state import Turn
from app.modules.assistant.domain.intents import (
    ApplianceType,
    ClassificationMethod,
    Intent,
    IntentMatch,
    detect_appliance,
)

_MAX_FOLLOW_UP_WORDS = 9
_MAX_QUOTED_CHARS = 200

# Openers that only make sense after another question. A bare "why" counts, "why is the sky blue" does not.
_LEAD = re.compile(
    r"^(?:(?:and|but|also|then|what about|how about|what if|really|more|explain|tell me more|"
    r"can you explain|is that|does that|will that)\b|(?:but\s+)?(?:why|how come)\s*[?.!]*$)"
)
_REFERS_BACK = re.compile(r"\b(?:it|that|this|those|these|they|them|there|same|again)\b")
# Hinglish and Hindi/Marathi openers and pointers. A bare "kyun?" / "क्यों?" is a follow-up exactly like a bare "why?".
# Devanagari has no `\b`, so these are anchored on the start of the question or on whitespace instead.
_LEAD_HI = re.compile(
    r"^(?:(?:aur|lekin|par|phir|toh|to|aur\s+kal|kal\s+ka|isliye|matlab)\b|(?:kyun|kyon|kaise|kaisa|kab)\s*[?.!]*$|"
    r"(?:और|लेकिन|फिर|तो|मतलब)(?:\s|$)|(?:क्यों|कैसे|कब|का|आणि|पण|मग)\s*[?.!]*$)"
)
_REFERS_BACK_HI = re.compile(
    r"\b(?:yeh|ye|woh|wo|vo|isse|usse|iska|uska|isme|usme|yahan|wahan|wahi|phir\s+se)\b|"
    r"(?:^|\s)(?:यह|ये|वह|वो|इसका|उसका|इसमें|उसमें|वही|हे|ते)(?:\s|$)"
)

# A turn that was refused or turned into an action is not a topic a follow-up can continue.
_NOT_A_TOPIC = frozenset({Intent.DEVICE_CONTROL.value, Intent.OUT_OF_SCOPE.value})


@dataclass(frozen=True)
class FollowUp:
    match: IntentMatch
    question: str  # the question plus the one it follows


def looks_like_follow_up(question: str) -> bool:
    text = " ".join(question.lower().split())
    if len(text.split()) > _MAX_FOLLOW_UP_WORDS:
        return False
    return bool(_LEAD.search(text) or _LEAD_HI.search(text) or _REFERS_BACK.search(text) or _REFERS_BACK_HI.search(text))


def resolve_follow_up(
    question: str, rules: IntentMatch, turns: Sequence[Turn]
) -> FollowUp | None:
    """The inherited routing for a follow-up, or None when the question stands on its own."""
    if rules.confident or not turns or not looks_like_follow_up(question):
        return None
    if rules.primary is not Intent.OUT_OF_SCOPE:
        # The rules found a domain word ("and the grid?") but no specific intent. The user named a new
        # subject, so an overview is a better answer than silently continuing the old topic.
        return None
    previous = turns[-1]
    if not previous["intents"] or previous["intents"][0] in _NOT_A_TOPIC:
        return None

    appliance = detect_appliance(question)
    if appliance is None and previous["appliance"]:
        appliance = ApplianceType(previous["appliance"])
    match = IntentMatch(
        intents=tuple(parse_intent(i) for i in previous["intents"]),
        appliance=appliance,
        confident=True,
        method=ClassificationMethod.MEMORY,
    )
    quoted = previous["question"][:_MAX_QUOTED_CHARS]
    return FollowUp(match=match, question=f'{question}\n(Follow-up to the earlier question: "{quoted}")')
