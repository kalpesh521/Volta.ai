"""
Gate G8, the checks that need no facts: things a model-written answer must never say (doc 04 §3 items 5 and 6).

* **Action claims.** The model has no way to change a device, so "I've switched it off" or "the geyser has
  been turned on" is false by construction. A device action is only ever stated by the deterministic wording
  of the Device Action agent, never by the model.
* **Unsafe electrical advice.** A short deny-list of instructions that are dangerous to follow.

Numbers are checked separately by the grounding guard. A violation here is not regenerated: the answer is
replaced by the agents' verified wording, which cannot contain either.

Personal data leaving in an answer is handled by `pii.masker.redact_leaks` and `restore`, in the supervisor's
`finalize` and the service.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_I = re.IGNORECASE
# A state change on a device: "turned off", "switched on", "shut down", "powered up". Deliberately not "started" or
# "switched to grid": those describe the data ("the inverter switched to grid mode"), not an action.
_STATE_CHANGE = r"(?:turned|switched|shut|powered)\s+(?:on|off|down|up)"
_SETTING_CHANGE = r"(?:scheduled|enabled|disabled|activated|deactivated)"
_WILL_DO = r"(?:turn|switch|shut|power|schedule|toggle)"


class Kind(StrEnum):
    ACTION_CLAIM = "action_claim"
    UNSAFE_ADVICE = "unsafe_advice"


@dataclass(frozen=True)
class Violation:
    kind: Kind
    rule: str


_RULES: tuple[tuple[Kind, str, re.Pattern[str]], ...] = (
    (
        Kind.ACTION_CLAIM,
        "first_person_past",
        re.compile(rf"\b(?:i|we)(?:'ve|\s+have|\s+just|\s+already|\s+successfully)?\s+(?:{_STATE_CHANGE}|{_SETTING_CHANGE})\b", _I),
    ),
    (
        Kind.ACTION_CLAIM,
        "first_person_future",
        re.compile(rf"\b(?:i|we)(?:'ll|\s+will|\s+am\s+going\s+to)\s+{_WILL_DO}\b", _I),
    ),
    (
        Kind.ACTION_CLAIM,
        "passive_completed",
        re.compile(rf"\b(?:has|have|had)\s+been\s+{_STATE_CHANGE}\b", _I),
    ),
    (
        Kind.UNSAFE_ADVICE,
        "open_enclosure",
        re.compile(r"\b(?:open|remove|take\s+off|unscrew)\s+(?:the\s+)?(?:inverter|battery|panel|meter)\s+(?:cover|casing|enclosure|housing|box)\b", _I),
    ),
    (
        Kind.UNSAFE_ADVICE,
        "bypass_protection",
        re.compile(r"\b(?:bypass|short|jump|disable|remove)\s+(?:the\s+)?(?:breaker|mcb|rccb|elcb|fuse|earthing|bms|battery\s+protection|surge\s+protection)\b", _I),
    ),
    (
        Kind.UNSAFE_ADVICE,
        "tamper_meter",
        re.compile(r"\b(?:tamper\s+with|bypass|reverse)\s+(?:the\s+)?(?:electricity\s+|energy\s+)?meter\b", _I),
    ),
)


def check_answer(answer: Mapping[str, str]) -> list[Violation]:
    """Violations found in the model-written fields. Empty means the answer may stand."""
    text = " ".join(str(value) for key, value in answer.items() if key != "data_time")
    return [Violation(kind, rule) for kind, rule, pattern in _RULES if pattern.search(text)]
