"""Deterministic alert rules. Delivery (WebSocket, email) happens after these are saved."""
from __future__ import annotations

from dataclasses import dataclass

Priority = str

PRIORITY_BY_KIND: dict[str, str] = {
    "low_battery": "critical",
    "grid_outage": "critical",
    "inverter_fault": "critical",
    "high_grid_import": "warning",
    "solar_generation_low": "warning",
    "sensor_offline": "warning",
    "device_command_failed": "warning",
    "grid_restored": "info",
    "action_waiting_confirmation": "info",
    "daily_energy_summary_ready": "info",
    "automation_suggestion": "info",
    "recommendation_ready": "info",
}


def priority_for(kind: str) -> str:
    return PRIORITY_BY_KIND.get(kind, "info")


def channels_for(priority: str) -> tuple[str, ...]:
    """In-app is the WebSocket toast. Email is optional and only for critical alerts."""
    if priority == "critical":
        return ("in_app", "email")
    return ("in_app",)


@dataclass(frozen=True)
class AlertDraft:
    kind: str
    title: str
    body: str
    priority: str


def _draft(kind: str, title: str, body: str) -> AlertDraft:
    return AlertDraft(kind, title, body, priority_for(kind))


def alerts_from_home(
    *,
    soc_percent: float | None,
    reserve_percent: float,
    grid_status: str | None,
    import_kw: float | None,
    solar_kw: float | None,
    radiation_wm2: float | None,
    data_quality: str | None,
    fault_code: str | None,
    previous_outage: bool = False,
    daily_ready: bool = False,
    insight: str | None = None,
) -> list[AlertDraft]:
    found: list[AlertDraft] = []
    grid = (grid_status or "").lower()
    outage = grid in {"outage", "off", "disconnected", "islanded"}
    if soc_percent is not None and (soc_percent <= reserve_percent or soc_percent <= 15):
        found.append(
            _draft(
                "low_battery",
                "Battery is very low",
                f"Charge is {soc_percent:.0f}%, at or below the {reserve_percent:.0f}% reserve.",
            )
        )
    if outage:
        found.append(_draft("grid_outage", "Grid outage", "The grid supply is not available."))
    elif previous_outage:
        found.append(_draft("grid_restored", "Grid restored", "The grid supply is available again."))
    if import_kw is not None and import_kw > 1.0:
        found.append(
            _draft(
                "high_grid_import",
                "High grid import",
                f"The home is importing {import_kw:.1f} kW from the grid.",
            )
        )
    if (
        solar_kw is not None
        and radiation_wm2 is not None
        and radiation_wm2 >= 500
        and solar_kw < 0.05
    ):
        found.append(
            _draft(
                "solar_generation_low",
                "Solar output is unusually low",
                "Sunlight is strong but solar power is near zero.",
            )
        )
    if (data_quality or "").lower() in {"missing", "disconnected", "degraded"}:
        found.append(
            _draft(
                "sensor_offline",
                "Energy data is incomplete",
                "A sensor or the telemetry feed is not healthy.",
            )
        )
    if fault_code:
        found.append(
            _draft(
                "inverter_fault",
                "Inverter or battery fault",
                f"Fault code {fault_code} is present. Check the manual before guessing a cause.",
            )
        )
    if daily_ready:
        found.append(
            _draft(
                "daily_energy_summary_ready",
                "Daily energy summary",
                "Today's energy summary is ready in the dashboard.",
            )
        )
    text = (insight or "").strip()
    if text:
        found.append(_draft("recommendation_ready", "Energy insight", text[:500]))
    return found
