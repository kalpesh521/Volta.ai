"""
What counts as personal data in Volta, and what to do about each class (doc 04 §4).

Two policies:

* `MASK`  the value is replaced by a token before the model or a checkpoint sees it. The question is still
          answered.
* `BLOCK` the message is refused and never stored. Card numbers, bank accounts, UPI ids and secrets have no
          place in an energy chat, so there is nothing safe to keep.

`show_in_answer` says whether a masked value may be put back into the reply the user reads. A consumer
number the user typed may come back; an email, phone or national id never does, even if the model repeats
its token.

Identifiers that look like ordinary numbers (consumer, meter and bank account numbers) are only recognised
next to their label. A bare run of digits is never treated as one of them.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.agents_platform.guardrails.pii import validators


class PiiClass(StrEnum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    AADHAAR = "AADHAAR"
    PAN = "PAN"
    IFSC = "IFSC"
    GPS = "GPS"
    CONSUMER_NO = "CONSUMER_NO"
    METER_NO = "METER_NO"
    CARD = "CARD"
    UPI = "UPI"
    BANK_ACCOUNT = "BANK_ACCOUNT"
    SECRET = "SECRET"


class Action(StrEnum):
    MASK = "mask"
    BLOCK = "block"


@dataclass(frozen=True)
class ClassPolicy:
    action: Action
    show_in_answer: bool
    label: str  # used in "[hidden <label>]"


POLICY: dict[PiiClass, ClassPolicy] = {
    PiiClass.EMAIL: ClassPolicy(Action.MASK, False, "email"),
    PiiClass.PHONE: ClassPolicy(Action.MASK, False, "phone number"),
    PiiClass.AADHAAR: ClassPolicy(Action.MASK, False, "id number"),
    PiiClass.PAN: ClassPolicy(Action.MASK, False, "id number"),
    PiiClass.IFSC: ClassPolicy(Action.MASK, False, "bank code"),
    PiiClass.GPS: ClassPolicy(Action.MASK, False, "location"),
    PiiClass.CONSUMER_NO: ClassPolicy(Action.MASK, True, "consumer number"),
    PiiClass.METER_NO: ClassPolicy(Action.MASK, True, "meter number"),
    PiiClass.CARD: ClassPolicy(Action.BLOCK, False, "card number"),
    PiiClass.UPI: ClassPolicy(Action.BLOCK, False, "UPI id"),
    PiiClass.BANK_ACCOUNT: ClassPolicy(Action.BLOCK, False, "account number"),
    PiiClass.SECRET: ClassPolicy(Action.BLOCK, False, "secret"),
}

# Overlaps are resolved in this order: the most specific, most sensitive reading wins.
_PRIORITY = (
    PiiClass.SECRET,
    PiiClass.CARD,
    PiiClass.BANK_ACCOUNT,
    PiiClass.CONSUMER_NO,
    PiiClass.METER_NO,
    PiiClass.UPI,
    PiiClass.AADHAAR,
    PiiClass.PAN,
    PiiClass.IFSC,
    PiiClass.EMAIL,
    PiiClass.PHONE,
    PiiClass.GPS,
)
_RANK = {cls: index for index, cls in enumerate(_PRIORITY)}


@dataclass(frozen=True)
class Detection:
    cls: PiiClass
    start: int
    end: int
    value: str


_I = re.IGNORECASE
_UPI_HANDLES = (
    "okaxis|oksbi|okhdfcbank|okicici|ybl|ibl|axl|paytm|apl|upi|axisbank|sbi|hdfcbank|icici|"
    "kotak|pnb|boi|cnrb|fbl|idfcbank|yesbank|rbl|airtel|jio|postbank"
)
_LABEL_SEP = r"\s*(?:is|:|-|=)?\s*#?\s*"

_SECRET_PATTERNS = (
    re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:password|passwd|pwd|otp|cvv)\b" + _LABEL_SEP + r"\S{3,}", _I),
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_UPI = re.compile(rf"(?<![\w.])[\w.-]{{2,}}@(?:{_UPI_HANDLES})\b", _I)
_PHONE = re.compile(r"(?<![\w.+])(?:(?:\+|00)?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\w])")
_AADHAAR = re.compile(r"(?<![\d.])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", _I)
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", _I)
_GPS = re.compile(r"(?<![\d.])(-?\d{1,3}\.\d{4,})\s*[,;]\s*(-?\d{1,3}\.\d{4,})(?![\d.])")
_CONSUMER = re.compile(
    r"\b(?:consumer|customer|connection|service)\s*(?:no\.?|number|num|id|#)" + _LABEL_SEP + r"(\d[\d -]{5,16}\d)", _I
)
_METER = re.compile(
    r"\bmeter\s*(?:no\.?|number|num|serial(?:\s*(?:no\.?|number))?|id)" + _LABEL_SEP + r"([A-Za-z0-9]{6,16})", _I
)
_BANK_ACCOUNT = re.compile(
    r"\b(?:a/c|acct?\.?|account)\s*(?:no\.?|number|num|#)?" + _LABEL_SEP + r"(\d[\d -]{7,18}\d)", _I
)


def _spans(pattern: re.Pattern[str], cls: PiiClass, text: str, *, group: int = 0,
           accept: Callable[[str], bool] | None = None) -> list[Detection]:
    found = []
    for match in pattern.finditer(text):
        value = match.group(group)
        if accept is None or accept(value):
            found.append(Detection(cls, match.start(group), match.end(group), value))
    return found


def _digit_count_between(low: int, high: int) -> Callable[[str], bool]:
    return lambda value: low <= len(validators.digits_only(value)) <= high


def _has_digits(minimum: int) -> Callable[[str], bool]:
    return lambda value: sum(c.isdigit() for c in value) >= minimum


def _gps_detections(text: str) -> list[Detection]:
    found = []
    for match in _GPS.finditer(text):
        if validators.gps_valid(float(match.group(1)), float(match.group(2))):
            found.append(Detection(PiiClass.GPS, match.start(), match.end(), match.group(0)))
    return found


def detect(text: str) -> list[Detection]:
    """Every personal-data span in `text`, non-overlapping, in reading order."""
    candidates: list[Detection] = []
    for pattern in _SECRET_PATTERNS:
        candidates += _spans(pattern, PiiClass.SECRET, text)
    candidates += _spans(_CARD, PiiClass.CARD, text, accept=validators.luhn_valid)
    candidates += _spans(_BANK_ACCOUNT, PiiClass.BANK_ACCOUNT, text, group=1, accept=_digit_count_between(9, 18))
    candidates += _spans(_CONSUMER, PiiClass.CONSUMER_NO, text, group=1, accept=_digit_count_between(7, 15))
    candidates += _spans(_METER, PiiClass.METER_NO, text, group=1, accept=_has_digits(4))
    candidates += _spans(_UPI, PiiClass.UPI, text)
    candidates += _spans(_AADHAAR, PiiClass.AADHAAR, text, accept=validators.aadhaar_valid)
    candidates += _spans(_PAN, PiiClass.PAN, text, accept=validators.pan_valid)
    candidates += _spans(_IFSC, PiiClass.IFSC, text, accept=validators.ifsc_valid)
    candidates += _spans(_EMAIL, PiiClass.EMAIL, text)
    candidates += _spans(_PHONE, PiiClass.PHONE, text)
    candidates += _gps_detections(text)

    chosen: list[Detection] = []
    for candidate in sorted(candidates, key=lambda d: (_RANK[d.cls], d.start)):
        if all(candidate.end <= kept.start or candidate.start >= kept.end for kept in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda d: d.start)
