"""
Virtual-device command result.

The HTTP API only queues a command. This function decides whether the
simulator can apply it. A separate consumer records the audit row.
"""
from __future__ import annotations

from typing import Any

VIRTUAL_DEVICES = frozenset(
    {
        "water_heater",
        "washing_machine",
        "air_conditioner",
        "water_pump",
        "ev_charger",
    }
)


def handle_device_command(payload: dict[str, Any]) -> dict[str, Any]:
    required = ("command_id", "household_id", "device", "command")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        return {"applied": False, "status": "invalid", "missing": missing}
    command = payload.get("command")
    if command not in {"on", "off", "schedule"}:
        return {"applied": False, "status": "invalid", "command_id": payload.get("command_id")}
    if command == "schedule" and not payload.get("duration_minutes"):
        return {"applied": False, "status": "invalid", "command_id": payload.get("command_id")}
    if payload["device"] not in VIRTUAL_DEVICES:
        return {
            "applied": False,
            "status": "no_device_adapter",
            "command_id": payload["command_id"],
            "household_id": payload["household_id"],
            "device": payload["device"],
            "command": command,
        }
    return {
        "applied": True,
        "status": "virtual_applied",
        "command_id": payload["command_id"],
        "household_id": payload["household_id"],
        "device": payload["device"],
        "command": "on" if command == "schedule" else command,
        "duration_minutes": payload.get("duration_minutes"),
        "power_kw": payload.get("power_kw"),
    }
