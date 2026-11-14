"""
Bridges from the DSL to the rest of the platform, without importing its DB models.

- `proposed_command` turns a `propose_device_action` into the (device, command, duration) triple the confirm-before-act
  workflow in `agents/device_action` already understands. `turn_on`/`turn_off` map to its "on"/"off". `set_level` has no
  counterpart in `safety.py`/`worker.py` today (they accept on/off/schedule), so the executor must either extend them or
  refuse; it is passed through as "set_level" with `level_percent` rather than silently coerced.
- `notification_for` renders a `notify` action into the fields of `HomeNotification`.
- `legacy_automation_fields` projects a rule onto the old `AutomationRule` columns (name, device, command, mode, enabled).
  That table cannot hold conditions, schedules or notify-only rules, so this returns None for those: the lead should
  store the full DSL JSON + fingerprint + state alongside (see the report), and use this only for the legacy row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents_platform.automation.dsl import NotifyAction, ProposeDeviceAction, Rule
from app.agents_platform.automation.templates import RenderedNotification, render_notification

_COMMANDS = {"turn_on": "on", "turn_off": "off", "set_level": "set_level"}
LEGACY_MODE = "dsl_v1"


@dataclass(frozen=True)
class ProposedCommand:
    device: str
    command: str  # "on" | "off" | "set_level"
    duration_minutes: int | None = None
    level_percent: float | None = None


def proposed_command(action: ProposeDeviceAction) -> ProposedCommand:
    return ProposedCommand(action.device, _COMMANDS[action.action], action.duration_minutes, action.level_percent)


def notification_for(action: NotifyAction, rule: Rule) -> RenderedNotification:
    return render_notification(action.template, action.params, rule.name, action.channel)


def legacy_automation_fields(rule: Rule) -> dict[str, Any] | None:
    for action in rule.actions:
        if isinstance(action, ProposeDeviceAction) and action.action in {"turn_on", "turn_off"}:
            return {
                "name": rule.name[:80],
                "device": action.device,
                "command": _COMMANDS[action.action],
                "surplus_kw_min": None,
                "soc_percent_min": None,
                "mode": LEGACY_MODE,
                "enabled": rule.enabled,
            }
    return None
