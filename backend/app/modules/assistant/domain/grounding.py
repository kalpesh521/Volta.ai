"""
Numeric grounding: every number in an LLM answer must trace back to the facts.

The assistant's rule is "the LLM explains, code calculates". The prompt asks the
model not to invent numbers; this module checks that it obeyed. An answer number
is grounded when it is a rounding (or truncation) of a number the model was
given, optionally in another unit (kW/W, fraction/percent, seconds/minutes/hours).

What is deliberately NOT checked, because it carries no energy claim:
  * clock times and dates (`10:30`, `3 PM`, `2026-11-09`) and four-digit years
  * unit-less whole numbers up to 10 (counts and ordinals: "2 devices", "step 1")

Pure Python and no I/O, so it is cheap enough to run on every answer.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_ANSWER_FIELDS = ("observation", "explanation", "recommendation", "estimated_impact")
_SMALL_INTEGER_LIMIT = 10
_YEAR_RANGE = range(1900, 2101)
_EPSILON = 1e-9

# Dates and timestamps are blanked before numbers are scanned.
_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?")

_NUMBER = re.compile(
    r"""
    (?<![\w.])                                  # not inside a word, an id like E04, or a longer number
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)             # 1,250 or 1250
    (?:\.(?P<frac>\d+))?
    (?P<clock>:\d{2}(?::\d{2})?)?               # 10:30 or 10:30:15
    \s*
    (?P<unit>
        (?:kilowatt[\s-]?hours?|kilowatts?|kwh|kwp|kw|watts?|wh|wp|w
          |hours?|hrs?|h|minutes?|mins?|min|seconds?|secs?|s
          |days?|a\.?m\.?|p\.?m\.?)(?![A-Za-z])
        |%
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)
_CURRENCY_BEFORE = re.compile(r"(?:₹|\brs\.?|\binr)\s*$", re.IGNORECASE)
_FACT_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Which fact values an answer number in a given unit may have come from.
_UNIT_FAMILY: dict[str, str] = {
    "w": "watt", "watt": "watt", "watts": "watt", "wh": "watt", "wp": "watt",
    "kw": "kilowatt", "kwh": "kilowatt", "kwp": "kilowatt", "kilowatt": "kilowatt", "kilowatts": "kilowatt",
    "%": "percent",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
    "day": "days", "days": "days",
}
# Facts are stored in kW/kWh and as percent values, so only the conversions the model can
# legitimately make are allowed. Reverse ones (W to kW, fraction to percent) would only add
# coincidental matches.
_FACTORS: dict[str, tuple[float, ...]] = {
    "watt": (1.0, 1000.0),                    # facts are kW, answer says W
    "hours": (1.0, 1 / 60, 1 / 3600),         # facts may be minutes or seconds
    "minutes": (1.0, 60.0, 1 / 60),           # facts may be hours or seconds
    "seconds": (1.0, 60.0, 3600.0),
    "days": (1.0, 1 / 24),
}
_DEFAULT_FACTORS = (1.0,)


@dataclass(frozen=True)
class GroundingResult:
    checked: int
    ungrounded: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.ungrounded


@dataclass(frozen=True)
class _Claim:
    text: str
    value: float
    decimals: int
    family: str | None


def collect_fact_numbers(*sources: Any) -> frozenset[float]:
    """Every number reachable in the given JSON-like structures, including numbers inside strings."""
    found: set[float] = set()
    for source in sources:
        _walk(source, found)
    return frozenset(found)


def check_grounding(
    answer: Mapping[str, Any],
    *,
    facts: Iterable[Any],
    question: str = "",
) -> GroundingResult:
    """
    Check the answer's text fields against `facts` (what the model was shown) and the user's
    own question (a number the user typed may be repeated back).
    """
    allowed = collect_fact_numbers(*facts, question)
    claims = [claim for name in _ANSWER_FIELDS for claim in _extract_claims(str(answer.get(name) or ""))]
    ungrounded = tuple(claim.text for claim in claims if not _is_grounded(claim, allowed))
    return GroundingResult(checked=len(claims), ungrounded=tuple(dict.fromkeys(ungrounded)))


def _walk(node: Any, out: set[float]) -> None:
    if node is None or isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        out.add(abs(float(node)))
    elif isinstance(node, str):
        for match in _FACT_NUMBER.finditer(_ISO_DATETIME.sub(lambda m: _digits_of(m.group()), node)):
            out.add(float(match.group().replace(",", "")))
    elif isinstance(node, Mapping):
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, (list, tuple, set, frozenset)):
        for value in node:
            _walk(value, out)


def _digits_of(timestamp: str) -> str:
    """Keep a timestamp's digits as separate numbers so '2026-11-09' allows 2026, 11 and 9."""
    return " ".join(re.findall(r"\d+", timestamp))


def _extract_claims(text: str) -> list[_Claim]:
    scrubbed = _ISO_DATETIME.sub(" ", text)
    claims: list[_Claim] = []
    for match in _NUMBER.finditer(scrubbed):
        unit = (match.group("unit") or "").lower().replace(" ", "").replace("-", "")
        if match.group("clock") or re.fullmatch(r"[ap]\.?m\.?", unit):
            continue  # a time of day

        integer, fraction = match.group("int"), match.group("frac")
        value = float(f"{integer.replace(',', '')}.{fraction}" if fraction else integer.replace(",", ""))
        family = _UNIT_FAMILY.get(unit)
        has_unit = bool(unit) or bool(_CURRENCY_BEFORE.search(scrubbed[: match.start()]))
        if not has_unit and fraction is None:
            whole = int(value)
            if whole <= _SMALL_INTEGER_LIMIT or whole in _YEAR_RANGE:
                continue
        claims.append(
            _Claim(
                text=scrubbed[match.start() : match.end()].strip(),
                value=value,
                decimals=len(fraction) if fraction else 0,
                family=family,
            )
        )
    return claims


def _is_grounded(claim: _Claim, allowed: frozenset[float]) -> bool:
    factors = _FACTORS.get(claim.family or "", _DEFAULT_FACTORS)
    return any(_is_rounding_of(claim, fact * factor) for fact in allowed for factor in factors)


def _is_rounding_of(claim: _Claim, source: float) -> bool:
    """True when `claim` is `source` rounded (half up) or truncated to the claim's precision."""
    if abs(source - claim.value) < _EPSILON:
        return True
    try:
        exact = Decimal(repr(source))
    except InvalidOperation:
        return False
    step = Decimal(1).scaleb(-claim.decimals)
    return any(
        abs(float(exact.quantize(step, rounding=mode)) - claim.value) < _EPSILON
        for mode in (ROUND_HALF_UP, ROUND_DOWN)
    )
