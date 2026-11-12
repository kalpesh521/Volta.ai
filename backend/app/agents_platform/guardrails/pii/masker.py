"""
Replace personal data with typed tokens, and put back only what may be shown.

    "my email is kp@x.com, consumer no 170012345678"
 →  "my email is <EMAIL_1>, consumer no <CONSUMER_NO_1>"

The same value gets the same token within a message, so the model keeps the sentence structure. Values of
a blocked class are not tokenised and never stored: such a message is refused whole.

Restoring is strict. A token comes back as its original value only when its class may be shown and the token
belongs to this request. Anything else (an email token, a token the model invented, a token of another
request) becomes `[hidden <label>]`, so a model that repeats a token cannot leak what it stands for.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.agents_platform.guardrails.pii.detectors import POLICY, Action, PiiClass, detect

_TOKEN = re.compile(r"<([A-Z]+(?:_[A-Z]+)*)_(\d{1,3})>")
REMOVED = "[removed]"


@dataclass(frozen=True)
class MaskResult:
    text: str
    # token -> original value, for masked classes only. Holds raw personal data: pass it straight to a
    # vault and drop it. `repr=False` keeps it out of logs and assertion messages.
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    counts: Mapping[str, int] = field(default_factory=dict)  # class -> how many, safe to log
    blocked: tuple[PiiClass, ...] = ()


def mask(text: str) -> MaskResult:
    detections = detect(text)
    if not detections:
        return MaskResult(text=text)

    tokens: dict[str, str] = {}
    issued: dict[tuple[PiiClass, str], str] = {}
    serial: Counter[PiiClass] = Counter()
    counts: Counter[str] = Counter()
    blocked: list[PiiClass] = []
    pieces: list[str] = []
    cursor = 0
    for found in detections:
        counts[found.cls.value] += 1
        pieces.append(text[cursor : found.start])
        cursor = found.end
        if POLICY[found.cls].action is Action.BLOCK:
            if found.cls not in blocked:
                blocked.append(found.cls)
            pieces.append(REMOVED)
            continue
        key = (found.cls, found.value)
        token = issued.get(key)
        if token is None:
            serial[found.cls] += 1
            token = f"<{found.cls.value}_{serial[found.cls]}>"
            issued[key] = token
            tokens[token] = found.value
        pieces.append(token)
    pieces.append(text[cursor:])
    return MaskResult(text="".join(pieces), tokens=tokens, counts=dict(counts), blocked=tuple(blocked))


def hidden(cls: PiiClass) -> str:
    return f"[hidden {POLICY[cls].label}]"


def restore(text: str, tokens: Mapping[str, str]) -> str:
    """Put back the values this request may show; hide every other personal-data token."""

    def replace(match: re.Match[str]) -> str:
        try:
            cls = PiiClass(match.group(1))
        except ValueError:
            return match.group(0)  # not ours (for example markup): leave it alone
        value = tokens.get(match.group(0))
        if value is not None and POLICY[cls].show_in_answer:
            return value
        return hidden(cls)

    return _TOKEN.sub(replace, text)


def redact_leaks(text: str) -> tuple[str, Counter[str]]:
    """
    Hide personal data in text the user is about to read, whatever its source (a document passage, a model
    that made one up). Classes the user may be shown, such as a consumer number, are left as they are.
    """
    found = [d for d in detect(text) if not POLICY[d.cls].show_in_answer]
    if not found:
        return text, Counter()
    counts: Counter[str] = Counter()
    pieces: list[str] = []
    cursor = 0
    for item in found:
        counts[item.cls.value] += 1
        pieces.append(text[cursor : item.start])
        pieces.append(hidden(item.cls))
        cursor = item.end
    pieces.append(text[cursor:])
    return "".join(pieces), counts
