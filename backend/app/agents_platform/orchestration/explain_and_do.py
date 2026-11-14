"""
"Explain and do": a question and a device command in one message (tracker 3.11).

    "Why is the battery draining so fast, and turn off the AC"

Device Action is an exclusive route: it never shares a plan with another agent, so before this change the whole
message became a device request and the question was never answered. The command must still go through the
confirm-before-act workflow on its own turn, so this does not run both at once. It splits the message instead:

* the **question** is answered normally, by the read agents;
* the **command** is not run. The answer ends with an offer: send it on its own and Suryaa will ask you to confirm.

Nothing is proposed, nothing is published and no safety check is skipped. A command is only ever acted on when it
arrives as its own message, exactly as today.

The split is deliberately narrow, because a wrong split changes what the assistant does with a command:

* the **head must be a question** (an interrogative opener or a question mark). An assertion in front of a command
  ("The battery is 90%, turn on the AC") is the constraint-bypass attack from doc 05, and stays a device request,
  which the safety layer then rejects against real telemetry;
* the **tail must be a command the device-control rules recognise** on its own, naming its device (a pronoun like
  "turn it on" would mean nothing when sent alone);
* the head must route confidently to a read intent on its own, so the router model is never needed.

When anything is unsure the message is left alone and behaves exactly as before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.assistant.domain.intents import Intent, IntentMatch, classify_by_rules

_MAX_OFFER_CHARS = 90

_COMMAND_VERBS = r"(?:turn|switch|shut|power|start|stop|run|schedule|set|enable|disable)"
# The command is the tail: it starts after a comma, semicolon, "and", "then" or a full stop.
_TAIL = re.compile(
    rf"(?:^|[,;.?]|\b(?:and|then|also)\b)\s*(?:(?:and|then|also)\s+)*(?P<tail>(?:please\s+)?{_COMMAND_VERBS}\s+\S.*)$",
    re.IGNORECASE,
)
_TRAILING_JOINER = re.compile(r"[\s,;.]*(?:\b(?:and|then|also|please)\b[\s,;.]*)+$", re.IGNORECASE)
_PRONOUN_OBJECT = re.compile(r"\b(?:it|them|that|this|those|these)\b", re.IGNORECASE)
_INTERROGATIVE = re.compile(
    r"^(?:why|what|how|when|where|which|who|is|are|am|do|does|did|can|could|will|would|should|has|have)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExplainAndDo:
    question: str  # what the read agents answer
    match: IntentMatch  # how that question routes
    command: str  # offered back to the user, never executed here


def _clean(text: str) -> str:
    return " ".join(text.split()).strip(" ,;.")


def split_explain_and_do(question: str) -> ExplainAndDo | None:
    """The question and the command inside a combined message, or None when it should be handled as it is today."""
    found = _TAIL.search(question)
    if found is None:
        return None
    command = _clean(re.sub(r"^please\s+|\s+please[\s.!]*$", "", found.group("tail"), flags=re.IGNORECASE))
    head = _clean(_TRAILING_JOINER.sub("", question[: found.start("tail")]))
    if not head or not command or len(command) > _MAX_OFFER_CHARS:
        return None
    if not (head.endswith("?") or _INTERROGATIVE.match(head)):
        return None  # an assertion before a command is not a question; leave it to the safety layer
    if _PRONOUN_OBJECT.search(command):
        return None  # "turn it on" means nothing once it is sent on its own: the offer must name the device
    if classify_by_rules(command).primary is not Intent.DEVICE_CONTROL:
        return None
    match = classify_by_rules(head)
    if not match.confident or match.primary in (Intent.DEVICE_CONTROL, Intent.OUT_OF_SCOPE):
        return None
    return ExplainAndDo(question=head, match=match, command=command)


def offer_text(command: str) -> str:
    """The sentence appended to the recommendation. Fixed wording around the user's own words."""
    return f'To go ahead with that, send "{command}" on its own and I will ask you to confirm it first.'
