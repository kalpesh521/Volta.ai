"""
Notification templates a rule may use. A rule never carries message text, only a template id and typed numbers.

WHY: free text written by an LLM (or smuggled in by a user prompt) would land in a push notification verbatim.
With a fixed catalogue the worst a bad rule can do is show one of these sentences with an in-range number.
`requires_metric` stops a template from lying: "battery is low" is only allowed on a rule that actually watches SOC.
Kinds and priorities come from `domain/alerts.py` so an automation notification looks like the built-in alert it echoes.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.modules.assistant.domain.alerts import priority_for


@dataclass(frozen=True)
class ParamSpec:
    name: str
    unit: str
    min_value: float
    max_value: float


@dataclass(frozen=True)
class TemplateSpec:
    id: str
    kind: str  # HomeNotification.kind (an alerts.py kind where one exists)
    title: str
    body: str  # str.format with params and `rule_name`
    params: tuple[ParamSpec, ...] = ()
    requires_metric: str | None = None


TEMPLATES: dict[str, TemplateSpec] = {
    t.id: t
    for t in (
        TemplateSpec(
            "battery_low", "low_battery", "Battery is low",
            "Battery charge fell below {threshold_percent:.0f}% (rule: {rule_name}).",
            (ParamSpec("threshold_percent", "%", 0, 100),), "battery.soc_percent",
        ),
        TemplateSpec(
            "battery_hot", "automation_alert", "Battery is warm",
            "Battery temperature went above {threshold_c:.0f} degC (rule: {rule_name}).",
            (ParamSpec("threshold_c", "degC", -20, 80),), "battery.temperature_c",
        ),
        TemplateSpec(
            "grid_outage", "grid_outage", "Grid outage",
            "The grid supply is not available (rule: {rule_name}).", (), "grid.available",
        ),
        TemplateSpec(
            "grid_restored", "grid_restored", "Grid restored",
            "The grid supply is available again (rule: {rule_name}).", (), "grid.available",
        ),
        TemplateSpec(
            "high_grid_import", "high_grid_import", "High grid import",
            "The home is importing more than {threshold_kw:.1f} kW from the grid (rule: {rule_name}).",
            (ParamSpec("threshold_kw", "kW", 0, 50),), "grid.import_kw",
        ),
        TemplateSpec(
            "rule_triggered", "automation_triggered", "Automation ran",
            "Your automation '{rule_name}' just triggered.",
        ),
    )
}


@dataclass(frozen=True)
class RenderedNotification:
    """Maps one-to-one onto `HomeNotification(kind, priority, title, body)`; `channel` picks delivery."""

    kind: str
    priority: str
    title: str
    body: str
    channel: str


def render_notification(template_id: str, params: dict[str, float], rule_name: str, channel: str) -> RenderedNotification:
    spec = TEMPLATES[template_id]
    body = spec.body.format(rule_name=rule_name, **params)[:500]  # HomeNotification.body is String(500)
    return RenderedNotification(spec.kind, priority_for(spec.kind), spec.title, body, channel)
