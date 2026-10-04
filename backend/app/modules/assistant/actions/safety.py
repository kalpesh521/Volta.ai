"""
Deterministic checks that run before a device command is queued.

Order after the user confirms: RBAC, kill switch, battery reserve, device
power, then the broker. Unsafe requests are rejected before confirmation so
the assistant never asks someone to approve turning off a refrigerator.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionFacts:
    question: str
    device: str | None
    command: str | None
    jailbreak: bool
    owner_ok: bool
    battery_present: bool
    soc_percent: float | None
    reserve_percent: float
    rated_power_kw: float | None
    inverter_capacity_kw: float | None
    critical: bool
    controllable: bool
    control_enabled: bool
    kill_switch: bool
    device_known: bool


@dataclass(frozen=True)
class ActionVerdict:
    decision: str
    reasons: tuple[str, ...]
    checks: dict[str, str]


def evaluate_action(facts: ActionFacts, *, confirmed: bool | None = None) -> ActionVerdict:
    """`confirmed` is None before the interrupt, True/False after the user answers."""
    if confirmed is False:
        return ActionVerdict("cancelled", ("user_declined",), _checks(facts, blocked=()))

    reasons: list[str] = []
    if facts.jailbreak:
        reasons.append("prompt_injection")
    if not facts.owner_ok:
        reasons.append("rbac")
    if facts.device is None or facts.command is None:
        reasons.append("unclear_action")
    elif not facts.device_known:
        reasons.append("unknown_device")
    if facts.critical and facts.command == "off":
        reasons.append("critical_device")
    if facts.command == "on" and facts.device and not facts.controllable:
        reasons.append("not_controllable")
    if facts.command == "on" and facts.battery_present:
        if facts.soc_percent is None:
            reasons.append("battery_unknown")
        elif facts.soc_percent <= facts.reserve_percent:
            reasons.append("battery_below_reserve")
    if (
        facts.command == "on"
        and facts.rated_power_kw is not None
        and facts.inverter_capacity_kw is not None
        and facts.rated_power_kw > facts.inverter_capacity_kw + 0.05
    ):
        reasons.append("exceeds_inverter_capacity")
    if facts.kill_switch or not facts.control_enabled:
        reasons.append("kill_switch")

    if reasons:
        return ActionVerdict("reject", tuple(dict.fromkeys(reasons)), _checks(facts, blocked=set(reasons)))
    if confirmed is True:
        return ActionVerdict("ready", (), _checks(facts, blocked=set()))
    return ActionVerdict("propose", (), _checks(facts, blocked=set()))


def explain(decision: str, reasons: tuple[str, ...] | list[str], device: str | None, command: str | None) -> str:
    labels = {
        "prompt_injection": "I won't follow instructions to ignore safety rules.",
        "rbac": "This home is not yours.",
        "unclear_action": "Say which device to turn on or off.",
        "unknown_device": "That device is not on this home.",
        "critical_device": "The refrigerator is critical and cannot be switched off.",
        "not_controllable": "That device is not controllable.",
        "battery_unknown": "Live battery data is missing, so I will not start a load.",
        "battery_below_reserve": "Battery charge is at or below the reserve, so I will not start this load.",
        "exceeds_inverter_capacity": "That device draws more than the inverter can supply.",
        "kill_switch": "Device control is switched off.",
        "user_declined": "You cancelled the action. Nothing was sent.",
        "broker_unavailable": "The command could not be queued. Nothing changed on the device.",
    }
    if decision == "queued":
        return (
            f"The request to turn {command} the {device or 'device'} is queued. "
            "The device has not changed until the worker reports a result."
        )
    if decision == "awaiting_confirmation":
        return (
            f"I can queue turning {command} the {device}. "
            "Confirm to continue. Nothing is sent until you confirm."
        )
    if decision == "cancelled":
        return labels["user_declined"]
    if not reasons:
        return "The action was not sent."
    return " ".join(labels.get(reason, reason) for reason in reasons)


def _checks(facts: ActionFacts, blocked: set[str]) -> dict[str, str]:
    reserve = "not_applicable"
    if facts.command == "on" and facts.battery_present:
        reserve = "fail" if {"battery_below_reserve", "battery_unknown"} & blocked else "pass"
    power = "not_applicable"
    if facts.command == "on":
        power = "fail" if "exceeds_inverter_capacity" in blocked else "pass"
    return {
        "prompt_injection": "fail" if "prompt_injection" in blocked else "pass",
        "rbac": "fail" if "rbac" in blocked else "pass",
        "critical_device": "fail" if "critical_device" in blocked else "pass",
        "kill_switch": "blocked" if "kill_switch" in blocked else "off",
        "battery_reserve": reserve,
        "device_power": power,
    }
