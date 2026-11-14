"""
The built-in alerts of `domain/alerts.py`, expressed as DSL rules so one engine runs both alerts and user automations.

WHY not just call `alerts_from_home`: that function is level-triggered (it returns the same alerts every tick while the
condition holds) and leaves de-duplication to the caller. The engine gives the same thresholds edge-trigger semantics,
cooldown and hysteresis. The thresholds are copied here (alerts.py keeps them as literals); a parity test replays both
over the same readings so they cannot drift silently.

Not expressible yet, and so not here: `solar_generation_low` (needs irradiance), `sensor_offline` (data quality),
`inverter_fault` (fault code) and `grid_restored` (a transition, not a level). They stay in `alerts_from_home`.
"""
from __future__ import annotations

from app.agents_platform.automation.dsl import Rule

BUILTIN_PREFIX = "builtin."


def builtin_alert_rules(*, reserve_percent: float = 15.0, sustained_import_seconds: int = 300) -> list[Rule]:
    low_battery_at = max(reserve_percent, 15.0)  # alerts.py: soc <= reserve OR soc <= 15
    return [
        Rule.model_validate({
            "id": f"{BUILTIN_PREFIX}low_battery",
            "name": "Low battery",
            "trigger": {"kind": "compare", "metric": "battery.soc_percent", "op": "<=", "value": low_battery_at},
            "cooldown_seconds": 3600,
            "hysteresis": 0.2,
            "actions": [{"kind": "notify", "channel": "push", "template": "battery_low",
                         "params": {"threshold_percent": low_battery_at}}],
        }),
        Rule.model_validate({
            "id": f"{BUILTIN_PREFIX}grid_outage",
            "name": "Grid outage",
            "trigger": {"kind": "compare", "metric": "grid.available", "op": "==", "value": 0},
            "cooldown_seconds": 900,
            "actions": [{"kind": "notify", "channel": "push", "template": "grid_outage"}],
        }),
        Rule.model_validate({
            "id": f"{BUILTIN_PREFIX}high_grid_import",
            "name": "High grid import",
            "trigger": {"kind": "compare", "metric": "grid.import_kw", "op": ">", "value": 1.0},
            "for_seconds": sustained_import_seconds,
            "cooldown_seconds": 1800,
            "hysteresis": 0.2,
            "actions": [{"kind": "notify", "channel": "app", "template": "high_grid_import",
                         "params": {"threshold_kw": 1.0}}],
        }),
    ]
