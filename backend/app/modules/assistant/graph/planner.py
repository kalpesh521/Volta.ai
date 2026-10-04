"""
Intent → execution plan.

Deterministic on purpose: tool selection is auditable, testable in evals, and
costs no tokens. A future agentic mode can hand the same tools to an LLM.

Each plan also declares:
- `details`: optional field groups the answer needs (see `domain.projection`);
  everything else beyond the core numbers stays out of the prompt.
- `requires_telemetry`: without any reading the question cannot be answered.
- `live_advice`: the answer advises on current readings, so stale data must be flagged.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.modules.assistant.domain.analytics import Analytic
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.domain.projection import Detail
from app.modules.assistant.tools.registry import ToolName

T = ToolName
A = Analytic
D = Detail


@dataclass(frozen=True)
class IntentPlan:
    tools: frozenset[ToolName]
    analytics: frozenset[Analytic]
    details: frozenset[Detail] = frozenset()
    requires_telemetry: bool = True
    live_advice: bool = True


def _plan(
    tools: Iterable[ToolName],
    analytics: Iterable[Analytic],
    details: Iterable[Detail] = (),
    *,
    requires_telemetry: bool = True,
    live_advice: bool = True,
) -> IntentPlan:
    return IntentPlan(
        frozenset(tools), frozenset(analytics), frozenset(details), requires_telemetry, live_advice
    )


_SETUP_DETAILS = (D.HARDWARE, D.BATTERY_LIMITS, D.GRID_CONTRACT, D.BILLING, D.APPLIANCES, D.LIFETIME)

INTENT_PLANS: dict[Intent, IntentPlan] = {
    Intent.FULL_OVERVIEW: _plan(
        tuple(ToolName),
        (
            A.SOLAR_SURPLUS,
            A.SELF_CONSUMPTION,
            A.ENERGY_INDEPENDENCE,
            A.BACKUP_DURATION,
            A.ESTIMATED_SAVINGS,
            A.ANOMALIES,
        ),
        tuple(Detail),
        requires_telemetry=False,
    ),
    Intent.HOME_PROFILE: _plan(
        (T.HOUSEHOLD_PROFILE, T.LIVE_ENERGY_STATE),
        (),
        _SETUP_DETAILS,
        requires_telemetry=False,
        live_advice=False,
    ),
    Intent.LIVE_OVERVIEW: _plan(
        (T.LIVE_ENERGY_STATE, T.BATTERY_STATUS, T.GRID_STATUS, T.DEVICE_READINGS, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.ANOMALIES),
    ),
    Intent.SOLAR_PRODUCTION: _plan(
        (T.LIVE_ENERGY_STATE, T.DAILY_ENERGY_SUMMARY, T.WEATHER_DATA, T.RECENT_TREND, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.SELF_CONSUMPTION, A.ANOMALIES),
        (D.HARDWARE, D.LIFETIME, D.WEATHER_DETAIL),
    ),
    Intent.BATTERY: _plan(
        (T.BATTERY_STATUS, T.LIVE_ENERGY_STATE, T.RECENT_TREND, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.BACKUP_DURATION, A.ANOMALIES),
        (D.BATTERY_LIMITS,),
    ),
    Intent.BACKUP: _plan(
        (T.BATTERY_STATUS, T.LIVE_ENERGY_STATE, T.GRID_STATUS, T.HOUSEHOLD_PROFILE),
        (A.BACKUP_DURATION, A.ANOMALIES),
        (D.BATTERY_LIMITS, D.APPLIANCES),
    ),
    Intent.GRID_IMPORT: _plan(
        (T.LIVE_ENERGY_STATE, T.GRID_STATUS, T.BATTERY_STATUS, T.DAILY_ENERGY_SUMMARY, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.ENERGY_INDEPENDENCE, A.ANOMALIES),
        (D.GRID_CONTRACT,),
    ),
    Intent.GRID_EXPORT: _plan(
        (T.LIVE_ENERGY_STATE, T.GRID_STATUS, T.BATTERY_STATUS, T.DAILY_ENERGY_SUMMARY, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.SELF_CONSUMPTION, A.ANOMALIES),
        (D.GRID_CONTRACT,),
    ),
    Intent.DEVICE_USAGE: _plan(
        (T.DEVICE_READINGS, T.LIVE_ENERGY_STATE, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.ANOMALIES),
        (D.DEVICE_DETAIL,),
    ),
    Intent.APPLIANCE_TIMING: _plan(
        (T.LIVE_ENERGY_STATE, T.BATTERY_STATUS, T.DEVICE_READINGS, T.WEATHER_DATA, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.APPLIANCE_FIT, A.BACKUP_DURATION, A.ANOMALIES),
        (D.BATTERY_LIMITS,),
    ),
    Intent.SAVINGS: _plan(
        (T.DAILY_ENERGY_SUMMARY, T.HOUSEHOLD_PROFILE),
        (A.ESTIMATED_SAVINGS, A.SELF_CONSUMPTION, A.ENERGY_INDEPENDENCE),
        (D.GRID_CONTRACT, D.BILLING, D.LIFETIME),
    ),
    Intent.USAGE_PATTERN: _plan(
        (T.HOURLY_ENERGY_SUMMARY, T.DAILY_ENERGY_SUMMARY, T.RECENT_TREND, T.HOUSEHOLD_PROFILE),
        (A.SELF_CONSUMPTION, A.ENERGY_INDEPENDENCE),
        (D.APPLIANCES,),
    ),
    Intent.WEATHER: _plan(
        (T.WEATHER_DATA, T.LIVE_ENERGY_STATE, T.HOUSEHOLD_PROFILE),
        (A.SOLAR_SURPLUS, A.ANOMALIES),
        (D.WEATHER_DETAIL,),
    ),
    Intent.DEVICE_CONTROL: _plan((), (), requires_telemetry=False, live_advice=False),
    Intent.DOCUMENT: _plan(
        (T.SEARCH_KNOWLEDGE, T.HOUSEHOLD_PROFILE),
        (),
        (D.HARDWARE, D.GRID_CONTRACT, D.BILLING),
        requires_telemetry=False,
        live_advice=False,
    ),
    Intent.OUT_OF_SCOPE: _plan((), (), requires_telemetry=False, live_advice=False),
}

_TOOL_ORDER = {name: i for i, name in enumerate(ToolName)}
_ANALYTIC_ORDER = {name: i for i, name in enumerate(Analytic)}
_DETAIL_ORDER = {name: i for i, name in enumerate(Detail)}


@dataclass(frozen=True)
class ExecutionPlan:
    tools: tuple[ToolName, ...]
    analytics: tuple[Analytic, ...]
    details: tuple[Detail, ...]
    requires_telemetry: bool
    live_advice: bool


def build_plan(intents: Iterable[Intent]) -> ExecutionPlan:
    """Union of the intents' plans. The first (primary) intent decides whether telemetry is mandatory."""
    tools: set[ToolName] = set()
    analytics: set[Analytic] = set()
    details: set[Detail] = set()
    requires_telemetry: bool | None = None
    live_advice = False
    for intent in intents:
        plan = INTENT_PLANS[intent]
        tools |= plan.tools
        analytics |= plan.analytics
        details |= plan.details
        if requires_telemetry is None:
            requires_telemetry = plan.requires_telemetry
        live_advice = live_advice or plan.live_advice
    requires_telemetry = bool(requires_telemetry)
    return ExecutionPlan(
        tools=tuple(sorted(tools, key=_TOOL_ORDER.__getitem__)),
        analytics=tuple(sorted(analytics, key=_ANALYTIC_ORDER.__getitem__)),
        details=tuple(sorted(details, key=_DETAIL_ORDER.__getitem__)),
        requires_telemetry=requires_telemetry,
        live_advice=live_advice,
    )
