"""
Per-question projection of what the answer model receives.

Tools decide which sections exist; projection decides which fields inside a
section are worth tokens for this question:

- Always dropped: identifiers, per-section timestamps (`data_freshness` carries
  the reading time), and values repeated in another section or in analytics.
- Opt-in `Detail` groups: setup and descriptive fields that only some
  questions need. The planner declares them per intent.
- Never dropped: the core numbers that analytics, guardrails and the grounding
  rules rely on (power, SOC, kWh, reserve, tariff, goal, priority).

Inputs are never mutated; the untrimmed context still feeds the deterministic
fallback and the API trace.
"""
from __future__ import annotations

import copy
from datetime import datetime
from enum import StrEnum
from typing import Any


class Detail(StrEnum):
    HARDWARE = "hardware"
    BATTERY_LIMITS = "battery_limits"
    GRID_CONTRACT = "grid_contract"
    BILLING = "billing"
    APPLIANCES = "appliances"
    LIFETIME = "lifetime"
    WEATHER_DETAIL = "weather_detail"
    DEVICE_DETAIL = "device_detail"


_DETAIL_FIELDS: dict[str, dict[Detail, tuple[str, ...]]] = {
    "household": {
        Detail.HARDWARE: ("panel_type", "panel_qty", "inverter_brand"),
        Detail.BATTERY_LIMITS: (
            "battery_backup_hours_target",
            "battery_max_charge_kw",
            "battery_max_discharge_kw",
        ),
        Detail.GRID_CONTRACT: (
            "meter_type",
            "sanctioned_load_kw",
            "discom",
            "tariff_type",
            "export_credit_inr_per_kwh",
        ),
        Detail.BILLING: ("avg_monthly_bill_inr",),
        Detail.APPLIANCES: ("appliances",),
    },
    "live_energy": {Detail.LIFETIME: ("solar_lifetime_kwh", "consumption_lifetime_kwh")},
    "grid": {Detail.LIFETIME: ("lifetime_import_kwh", "lifetime_export_kwh")},
    "weather": {
        Detail.WEATHER_DETAIL: (
            "humidity_percent",
            "precipitation_mm",
            "wind_speed_kmh",
            "direct_radiation_wm2",
            "diffuse_radiation_wm2",
        ),
    },
}

_ALWAYS_DROP: dict[str, tuple[str, ...]] = {
    "live_energy": ("timestamp", "data_source", "location", "timezone"),
    "battery": ("timestamp",),
    "grid": ("timestamp",),
    "devices": ("timestamp",),
    "weather": ("timestamp", "source"),
    "today_summary": ("timezone", "reading_count", "period_end"),
    "hourly_summary": ("timezone",),
    "recent_trend": ("samples",),
}

_DEVICE_DETAIL_FIELDS = ("rated_power_kw", "controllable")


def _drop(section: dict[str, Any] | None, keys: tuple[str, ...]) -> None:
    if section:
        for key in keys:
            section.pop(key, None)


def _drop_if_same(section: dict[str, Any] | None, key: str, value: Any) -> None:
    if section and value is not None and section.get(key) == value:
        section.pop(key)


def _clock(value: Any) -> Any:
    try:
        return datetime.fromisoformat(str(value)).strftime("%H:%M")
    except ValueError:
        return value


def project_facts(facts: dict[str, Any], details: frozenset[Detail] | set[Detail]) -> dict[str, Any]:
    """Return the facts trimmed to what this question needs."""
    out = copy.deepcopy(facts)
    out.pop("preferences", None)  # primary_goal is in household; read-only is a system rule

    for section, groups in _DETAIL_FIELDS.items():
        for detail, keys in groups.items():
            if detail not in details:
                _drop(out.get(section), keys)
    for section, keys in _ALWAYS_DROP.items():
        _drop(out.get(section), keys)

    household = out.get("household") or {}
    live = out.get("live_energy")
    battery = out.get("battery")

    for appliance in household.get("appliances", []):
        _drop(appliance, ("type", "critical", "controllable"))

    if live and live.get("warnings"):
        # Simulator balance warnings repeat energy_balance_status / energy_balance_error_kw.
        live["warnings"] = [w for w in live["warnings"] if not str(w).startswith("energy_balance")]
        if not live["warnings"]:
            live.pop("warnings")

    devices = out.get("devices")
    if devices:
        for row in devices.get("devices", []):
            _drop(row, ("device_id", "type", "critical"))
            if Detail.DEVICE_DETAIL not in details:
                _drop(row, _DEVICE_DETAIL_FIELDS)

    weather = out.get("weather")
    if weather:
        _drop_if_same(weather, "data_quality", (live or {}).get("data_quality"))
        for key in ("sunrise", "sunset"):
            if key in weather:
                weather[key] = _clock(weather[key])

    daily = out.get("today_summary")
    for key in ("tariff_rate", "export_credit_inr_per_kwh"):
        _drop_if_same(daily, key, household.get(key))

    trend = out.get("recent_trend")
    if trend and battery:
        _drop_if_same(trend, "battery_soc_now_percent", battery.get("soc_percent"))

    return out


def project_analytics(analytics: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Drop analytics inputs that the projected facts already carry; keep every derived value."""
    out = copy.deepcopy(analytics)
    household = facts.get("household") or {}
    live = facts.get("live_energy") or {}
    battery = facts.get("battery") or {}
    daily = facts.get("today_summary") or {}

    surplus = out.get("solar_surplus")
    _drop_if_same(surplus, "solar_kw", live.get("solar_kw"))
    _drop_if_same(surplus, "load_kw", live.get("load_kw"))

    backup = out.get("backup")
    if backup:
        _drop_if_same(backup, "battery_present", True)
        _drop_if_same(backup, "soc_percent", battery.get("soc_percent"))
        _drop_if_same(backup, "load_kw", live.get("load_kw"))
        _drop_if_same(backup, "reserve_soc_percent", household.get("battery_minimum_soc_percent"))

    for name, keys in (
        ("self_consumption", ("solar_generation_kwh", "grid_export_kwh")),
        ("energy_independence", ("home_consumption_kwh", "grid_import_kwh")),
    ):
        for key in keys:
            _drop_if_same(out.get(name), key, daily.get(key))

    savings = out.get("savings")
    if savings:
        _drop_if_same(savings, "available", True)
        _drop_if_same(savings, "tariff_rate_inr_per_kwh", household.get("tariff_rate"))
        _drop_if_same(
            savings, "export_credit_inr_per_kwh", household.get("export_credit_inr_per_kwh")
        )

    for anomaly in out.get("anomalies") or []:
        anomaly.pop("code", None)

    appliance = out.get("appliance")
    if appliance:
        _drop(appliance, ("appliance", "rating_source"))
        _drop_if_same(appliance, "solar_surplus_kw", (surplus or {}).get("surplus_kw"))

    return out


def project_freshness(freshness: dict[str, Any]) -> dict[str, Any]:
    """Status and age are what the rules use; the reading time is stamped by finalize."""
    return {
        key: freshness[key]
        for key in ("status", "age_seconds", "message")
        if freshness.get(key) is not None
    }


def project_prompt(
    facts: dict[str, Any],
    analytics: dict[str, Any],
    freshness: dict[str, Any],
    details: frozenset[Detail] | set[Detail],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Projected (facts, analytics, freshness) for the answer prompt."""
    prompt_facts = project_facts(facts, details)
    prompt_analytics = project_analytics(analytics, prompt_facts)
    if (prompt_analytics.get("savings") or {}).get("method") and prompt_facts.get("today_summary"):
        prompt_facts["today_summary"].pop("savings_method", None)
    return prompt_facts, prompt_analytics, project_freshness(freshness)
