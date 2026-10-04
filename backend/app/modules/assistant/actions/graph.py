"""
LangGraph interrupt for device confirmation.

    parse → load → decide → confirm → load → decide → publish
                              (pause)

`interrupt` saves the checkpoint under the proposal id and returns only when
`Command(resume=...)` is sent with the same thread id. Rejected actions never
reach the interrupt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.core.config import Settings
from app.core.exceptions import EnergyNotFoundError
from app.modules.assistant.actions.parse import parse_control_request
from app.modules.assistant.actions.publisher import CommandBrokerError, CommandPublisher, DeviceCommand, new_command_id
from app.modules.assistant.actions.safety import ActionFacts, evaluate_action
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem

PARSE = "parse"
LOAD = "load"
DECIDE = "decide"
CONFIRM = "confirm"
PUBLISH = "publish"


class ActionState(TypedDict, total=False):
    proposal_id: str
    question: str
    household_id: str
    user_id: str
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
    device_known: bool
    decision: str
    reasons: list[str]
    checks: dict[str, str]
    confirmed: bool
    command_id: str | None


@dataclass
class ActionRuntime:
    energy: EnergyService
    system: SolarSystem
    publisher: CommandPublisher
    settings: Settings


def build_action_graph(checkpointer: MemorySaver) -> CompiledStateGraph:
    graph = StateGraph(ActionState, context_schema=ActionRuntime)
    graph.add_node(PARSE, parse_node)
    graph.add_node(LOAD, load_node)
    graph.add_node(DECIDE, decide_node)
    graph.add_node(CONFIRM, confirm_node)
    graph.add_node(PUBLISH, publish_node)
    graph.add_edge(START, PARSE)
    graph.add_edge(PARSE, LOAD)
    graph.add_edge(LOAD, DECIDE)
    graph.add_conditional_edges(DECIDE, route_after_decide, {CONFIRM: CONFIRM, PUBLISH: PUBLISH, END: END})
    graph.add_edge(CONFIRM, LOAD)
    graph.add_edge(PUBLISH, END)
    return graph.compile(checkpointer=checkpointer, name="suryaa_actions")


def parse_node(state: dict) -> dict[str, Any]:
    parsed = parse_control_request(state["question"])
    return {"device": parsed.device, "command": parsed.command, "jailbreak": parsed.jailbreak}


async def load_node(state: dict, runtime: Runtime[ActionRuntime]) -> dict[str, Any]:
    ctx = runtime.context
    system = ctx.system
    live = None
    try:
        live = await ctx.energy.get_live(system.household_id, system.location)
    except EnergyNotFoundError:
        live = None
    device = _match_device(live.devices if live else [], state.get("device"))
    battery = system.battery_config
    reserve = float(battery.reserve_pct) if battery is not None else 20.0
    critical = bool(device and device.critical) or state.get("device") == "refrigerator"
    controllable = bool(device.controllable) if device is not None else state.get("device") != "refrigerator"
    rated = float(device.rated_power_kw) if device is not None else None
    return {
        "owner_ok": str(system.user_id) == state.get("user_id"),
        "battery_present": battery is not None,
        "soc_percent": float(live.battery.soc_percent) if live is not None else None,
        "reserve_percent": reserve,
        "rated_power_kw": rated,
        "inverter_capacity_kw": float(system.inverter_capacity_kw),
        "critical": critical,
        "controllable": controllable,
        "device_known": device is not None,
    }


def decide_node(state: dict, runtime: Runtime[ActionRuntime]) -> dict[str, Any]:
    facts = ActionFacts(
        question=state.get("question", ""),
        device=state.get("device"),
        command=state.get("command"),
        jailbreak=bool(state.get("jailbreak")),
        owner_ok=bool(state.get("owner_ok")),
        battery_present=bool(state.get("battery_present")),
        soc_percent=state.get("soc_percent"),
        reserve_percent=float(state.get("reserve_percent") or 0),
        rated_power_kw=state.get("rated_power_kw"),
        inverter_capacity_kw=state.get("inverter_capacity_kw"),
        critical=bool(state.get("critical")),
        controllable=bool(state.get("controllable", True)),
        control_enabled=runtime.context.settings.DEVICE_CONTROL_ENABLED,
        kill_switch=runtime.context.settings.DEVICE_KILL_SWITCH,
        device_known=bool(state.get("device_known")),
    )
    confirmed = state.get("confirmed")
    verdict = evaluate_action(facts, confirmed=confirmed if "confirmed" in state else None)
    return {"decision": verdict.decision, "reasons": list(verdict.reasons), "checks": verdict.checks}


def confirm_node(state: dict) -> dict[str, Any]:
    answer = interrupt(
        {
            "proposal_id": state.get("proposal_id"),
            "device": state.get("device"),
            "command": state.get("command"),
            "checks": state.get("checks") or {},
        }
    )
    confirmed = bool(isinstance(answer, dict) and answer.get("confirmed"))
    return {"confirmed": confirmed}


async def publish_node(state: dict, runtime: Runtime[ActionRuntime]) -> dict[str, Any]:
    settings = runtime.context.settings
    if settings.DEVICE_KILL_SWITCH or not settings.DEVICE_CONTROL_ENABLED:
        checks = dict(state.get("checks") or {})
        checks["kill_switch"] = "blocked"
        return {"decision": "reject", "reasons": ["kill_switch"], "checks": checks, "command_id": None}
    command = DeviceCommand(
        command_id=new_command_id(),
        household_id=state["household_id"],
        user_id=state["user_id"],
        device=state["device"],
        command=state["command"],
    )
    try:
        command_id = await runtime.context.publisher.publish(command)
    except CommandBrokerError:
        return {"decision": "failed", "reasons": ["broker_unavailable"], "command_id": None}
    return {"decision": "queued", "reasons": [], "command_id": command_id}


def route_after_decide(state: dict) -> str:
    decision = state.get("decision")
    if decision == "propose":
        return CONFIRM
    if decision == "ready":
        return PUBLISH
    return END


def _match_device(devices: list, device_type: str | None):
    if not device_type:
        return None
    for device in devices:
        if device.device_type == device_type or device_type.replace("_", " ") in device.device_name.lower():
            return device
    return None
