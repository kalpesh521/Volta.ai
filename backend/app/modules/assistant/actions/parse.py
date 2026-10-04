"""Parse a control request. The numbers in the question are not trusted; live tools supply SOC."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.assistant.domain.intents import ApplianceType, detect_appliance

_JAILBREAK = re.compile(
    r"\bignore\b.{0,80}\b(?:rules|instructions|policy|policies|safety)\b"
    r"|\bdisregard\b.{0,80}\b(?:rules|instructions)\b"
    r"|\b(?:jailbreak|developer mode)\b"
    r"|\byou are now\b",
    re.I | re.S,
)
_OFF = re.compile(
    r"\b(?:turn|switch|shut|power)\s+(?:it\s+|the\s+\w+(?:\s+\w+){0,3}\s+)?off\b|\b(?:stop|disable)\b",
    re.I,
)
_ON = re.compile(
    r"\b(?:turn|switch|power)\s+(?:it\s+|the\s+\w+(?:\s+\w+){0,3}\s+)?on\b|\b(?:start|enable)\b",
    re.I,
)

_NAMES = {
    ApplianceType.WATER_HEATER: "water_heater",
    ApplianceType.WASHING_MACHINE: "washing_machine",
    ApplianceType.AIR_CONDITIONER: "air_conditioner",
    ApplianceType.EV_CHARGER: "ev_charger",
    ApplianceType.WATER_PUMP: "water_pump",
    ApplianceType.REFRIGERATOR: "refrigerator",
}


@dataclass(frozen=True)
class ParsedAction:
    device: str | None
    command: str | None
    jailbreak: bool


def parse_control_request(question: str) -> ParsedAction:
    appliance = detect_appliance(question)
    return ParsedAction(
        device=_NAMES.get(appliance) if appliance else None,
        command=_command(question),
        jailbreak=bool(_JAILBREAK.search(question)),
    )


def _command(question: str) -> str | None:
    off = _OFF.search(question)
    on = _ON.search(question)
    if off and on:
        return "off" if off.start() > on.start() else "on"
    if off:
        return "off"
    if on:
        return "on"
    return None
