"""
Explicit "remember" commands, recognised by rules (no model, no cost, no surprises).

    "Please remember that I run the washing machine after 11 am"   → "I run the washing machine after 11 am"
    "yaad rakhna ki geyser subah 6 baje chalta hai"                → "geyser subah 6 baje chalta hai"

Precision beats recall: a missed command costs the user one retry, a false positive would save something
they never asked to save. So the rules are anchored at the start of the message (or, for Hinglish, at its end),
and these are deliberately *not* commands:

    "do you remember what I said?"      a question
    "remember to switch off the geyser" a reminder request, not a fact (no reminder feature yet)
    "I remember when the inverter tripped", "remember when ..."   reminiscing
    "don't remember that", "forget that" the opposite of a command

The function only extracts the text. The caller still sends it through the safety gate in `service.py`.
"""
from __future__ import annotations

import re

from app.agents_platform.memory_store.models import MemoryKind

# Longer text is cut before matching: keeps the regexes cheap on hostile input. Anything this long is far above
# the item limit and will be refused for its length anyway.
_SCAN_LIMIT = 1000

_FLAGS = re.IGNORECASE | re.UNICODE
_LEAD = r"^\s*(?:(?:hey|hi|hello|ok|okay)\s+)?(?:suryaa\s*[,:\-]?\s+)?(?:(?:please|pls|plz|kindly)\s+)?"
_POLITE = r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+|pls\s+|kindly\s+)?)?"

# "remember that X", "remember this: X", "remember: X"
_EN_MARKED = re.compile(
    _LEAD + _POLITE + r"remember\s*(?:(?:that|this)\b\s*[:,\-]?|[:,\-])\s*(?P<body>.+)$",
    _FLAGS | re.DOTALL,
)
# "remember I use the geyser at 6 am": the "that" is dropped, so require a first-person or fact-like start.
_EN_BARE = re.compile(
    _LEAD
    + _POLITE
    + r"remember\s+(?P<body>(?:i|i'm|i've|i'd|my|we|we're|we've|our|the|there|this|it|it's)\b.+)$",
    _FLAGS | re.DOTALL,
)
_HI_LEAD = re.compile(
    r"^\s*(?:(?:please|pls|plz)\s+)?(?:(?:ye|yeh|ek\s+baat|ek\s+cheez)\s+)?"
    r"(?:yaad\s+(?:rakhna|rakho|rakhiye|rakhiyega|rakh\s*lo|rakh\s*lena)|याद\s+रखना|याद\s+रखो)"
    r"\s*(?:ki\b|ke\b|कि|:|,|\-)?\s*(?P<body>.+)$",
    _FLAGS | re.DOTALL,
)
# Postfix, common in Hinglish: "geyser subah 6 baje chalta hai, yaad rakhna"
_HI_TRAIL = re.compile(
    r"^(?P<body>.+?)[\s,;:\-.]+(?:yaad\s+(?:rakhna|rakho|rakhiye|rakh\s*lena)|याद\s+रखना)\s*[.!]*\s*$",
    _FLAGS | re.DOTALL,
)

# After a marker these are not facts about the home: reminders, memories, questions.
_NOT_A_FACT = re.compile(
    r"^(?:to\b|when\b|how\b|what\b|why\b|whether\b|if\b|me\b|us\b|everything\b|anything\b|nothing\b|"
    r"the\s+time\b|the\s+day\b|the\s+last\b|not\s+to\b|never\b)",
    _FLAGS,
)
_NEGATED_LEAD = re.compile(r"^\s*(?:please\s+)?(?:don'?t|do\s+not|never|stop|nahi|mat)\b", _FLAGS)

_TRAILING = " \t\r\n.!;,"


def _clean(body: str) -> str | None:
    body = " ".join(body.split()).strip(_TRAILING)
    if not body or _NOT_A_FACT.match(body):
        return None
    return body


def extract_remember_command(text: str) -> str | None:
    """The text the user asked Suryaa to remember, or None when the message is not a remember command."""
    if not isinstance(text, str):
        return None
    scanned = text[:_SCAN_LIMIT].strip()
    if not scanned or scanned.endswith("?") or _NEGATED_LEAD.match(scanned):
        return None
    for pattern in (_EN_MARKED, _EN_BARE, _HI_LEAD, _HI_TRAIL):
        match = pattern.match(scanned)
        if match:
            return _clean(match.group("body"))
    return None


# ---- a first guess at the kind; the user can change it on the page ------------------------------------------------

_EQUIPMENT = re.compile(
    r"\b(?:battery|inverter|panels?|solar\s+system|ev\s+charger|charger|net\s+meter|kwh|kw|kwp|installed|added|"
    r"upgraded|bought)\b",
    _FLAGS,
)
_GOAL = re.compile(
    r"\b(?:goal|aim|target|want\s+to\s+(?:reduce|save|cut|lower|reach)|reduce\s+(?:my\s+|the\s+)?(?:bill|cost)|"
    r"save\s+(?:money|more)|lower\s+(?:my\s+)?bill|zero\s+bill|backup\s+first)\b",
    _FLAGS,
)
_APPLIANCE = re.compile(
    r"\b(?:geyser|water\s+heater|ac|a/c|air\s*conditioner|washing\s+machine|dishwasher|fridge|refrigerator|"
    r"pump|motor|oven|microwave|iron|heater|cooler|fan|laundry|tv|lights?)\b.*\b(?:use[sd]?|run[s]?|ran|"
    r"on|off|start|stop|chalta|chalti|chalu|band|am|pm|baje|morning|evening|night|daily|weekday|weekend)\b",
    _FLAGS,
)
_STATED_PREFERENCE = re.compile(r"\b(?:prefer|prefers|like|likes|love|hate|dislike|pasand)\b", _FLAGS)
_HABIT_WORD = re.compile(r"\b(?:always|never|usually|avoid|rather)\b", _FLAGS)


def infer_kind(text: str) -> MemoryKind:
    """Keyword guess, in this order. A wrong guess is harmless: the kind is a label, and it can be edited."""
    if _GOAL.search(text):
        return MemoryKind.GOAL
    if _EQUIPMENT.search(text):
        return MemoryKind.EQUIPMENT
    if _STATED_PREFERENCE.search(text):
        return MemoryKind.PREFERENCE
    if _APPLIANCE.search(text):
        return MemoryKind.APPLIANCE_HABIT
    if _HABIT_WORD.search(text):
        return MemoryKind.PREFERENCE
    return MemoryKind.OTHER
