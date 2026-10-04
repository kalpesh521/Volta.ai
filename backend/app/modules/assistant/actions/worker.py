"""
Device-command worker boundary.

The HTTP API only queues a command. This function is what a worker runs when
it receives that message. There is no device adapter in this process, so the
result is explicit: the appliance was not changed.
"""
from __future__ import annotations

from typing import Any


def handle_device_command(payload: dict[str, Any]) -> dict[str, Any]:
    required = ("command_id", "household_id", "device", "command")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        return {"applied": False, "status": "invalid", "missing": missing}
    if payload.get("command") not in {"on", "off"}:
        return {"applied": False, "status": "invalid", "command_id": payload.get("command_id")}
    return {
        "applied": False,
        "status": "no_device_adapter",
        "command_id": payload["command_id"],
        "household_id": payload["household_id"],
        "device": payload["device"],
        "command": payload["command"],
    }
