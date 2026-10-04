"""
Answer contract and deterministic answer composer.

`AnswerDraft` is both the structured output requested from the LLM and the
shape of template answers used when the LLM is skipped (blocked intent, no
data) or unavailable (no key, provider outage). Template answers only state
facts and analytics results, so degraded mode is still grounded.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from app.modules.assistant.domain.analytics import (
    AnalyticsBundle,
    ApplianceAdvice,
    ApplianceVerdict,
    Severity,
)
from app.modules.assistant.domain.context import AIContext
from app.modules.assistant.domain.intents import Intent


class AnswerDraft(BaseModel):
    observation: str = Field(description="What the data shows, quoting exact values from the facts.")
    explanation: str = Field(description="Why it is happening, based only on the facts and analytics.")
    recommendation: str = Field(description="What the user can do. Never claim a device was changed.")
    estimated_impact: str = Field(
        description="Energy or cost effect taken from the analytics, or say it cannot be estimated."
    )


class FallbackReason(StrEnum):
    DEVICE_CONTROL = "device_control"
    OUT_OF_SCOPE = "out_of_scope"
    NO_DATA = "no_data"
    KNOWLEDGE_GAP = "knowledge_gap"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_ERROR = "llm_error"


_VERDICT_ADVICE: dict[ApplianceVerdict, str] = {
    ApplianceVerdict.RUN_NOW_ON_SOLAR: "Yes — now is a good time; solar can cover it.",
    ApplianceVerdict.RUN_WITH_BATTERY_SUPPORT: (
        "You can run it now; solar plus battery can cover it without grid import."
    ),
    ApplianceVerdict.WAIT_FOR_SOLAR: "Wait for stronger solar, ideally late morning to early afternoon.",
    ApplianceVerdict.GRID_IMPORT_REQUIRED: (
        "Running it now will draw from the grid; delay it to solar hours if it is not urgent."
    ),
    ApplianceVerdict.NOT_RECOMMENDED: "Avoid running it now to protect your battery reserve.",
    ApplianceVerdict.ALREADY_RUNNING: "It is already running; no change is needed.",
    ApplianceVerdict.INSUFFICIENT_DATA: "Tell me which appliance you mean, or check back when live data is available.",
}


def _fmt(value: float | None, unit: str, digits: int = 2) -> str:
    return "unavailable" if value is None else f"{value:.{digits}f} {unit}"


def _live_sentence(ctx: AIContext) -> str | None:
    live = ctx.live_energy
    if live is None:
        return None
    parts = [f"Solar is producing {live.solar_kw:.2f} kW and the home is using {live.load_kw:.2f} kW"]
    if ctx.battery is not None:
        parts.append(f"battery at {ctx.battery.soc_percent:.0f}% ({ctx.battery.status})")
    if ctx.grid is not None:
        if ctx.grid.import_kw > 0.02:
            parts.append(f"importing {ctx.grid.import_kw:.2f} kW from the grid")
        elif ctx.grid.export_kw > 0.02:
            parts.append(f"exporting {ctx.grid.export_kw:.2f} kW to the grid")
        else:
            parts.append(f"grid {ctx.grid.status} with no exchange")
    return ", ".join(parts) + "."


def _daily_sentence(ctx: AIContext) -> str | None:
    daily = ctx.today_summary
    if daily is None or daily.reading_count == 0:
        return None
    return (
        f"On {daily.date} you generated {daily.solar_generation_kwh:.2f} kWh, used "
        f"{daily.home_consumption_kwh:.2f} kWh, imported {daily.grid_import_kwh:.2f} kWh and "
        f"exported {daily.grid_export_kwh:.2f} kWh."
    )


def _surplus_explanation(analytics: AnalyticsBundle) -> str | None:
    surplus = analytics.solar_surplus
    if surplus is None:
        return None
    if surplus.state == "surplus":
        return (
            f"Solar exceeds home demand by {surplus.surplus_kw:.2f} kW, so the extra charges the "
            "battery or flows to the grid."
        )
    if surplus.state == "deficit":
        return (
            f"Home demand is {abs(surplus.surplus_kw):.2f} kW above solar output, so the gap is "
            "covered by the battery or the grid."
        )
    return "Solar output and home demand are roughly balanced."


def _top_anomaly(analytics: AnalyticsBundle) -> str | None:
    for anomaly in analytics.anomalies or []:
        if anomaly.severity in (Severity.CRITICAL, Severity.WARNING):
            return anomaly.message
    return None


def _appliance_answer(advice: ApplianceAdvice) -> tuple[str, str, str]:
    name = advice.device_name or "the appliance"
    observation = (
        f"{name} is rated about {_fmt(advice.rated_power_kw, 'kW')}; current solar surplus is "
        f"{_fmt(advice.solar_surplus_kw, 'kW')}."
    )
    recommendation = _VERDICT_ADVICE[advice.verdict]
    if advice.estimated_grid_cost_inr_per_hour is not None:
        impact = (
            f"About {_fmt(advice.expected_grid_import_kw, 'kW')} from the grid, roughly "
            f"₹{advice.estimated_grid_cost_inr_per_hour:.2f} per hour of running."
        )
    elif advice.expected_grid_import_kw == 0.0:
        impact = "No grid import expected while it runs."
    else:
        impact = "Not enough data to estimate the cost."
    return observation, recommendation, impact


def _home_setup(ctx: AIContext) -> tuple[str, str] | None:
    """(setup headline, configuration details) from onboarding facts."""
    h = ctx.household
    if h is None:
        return None
    where = f" in {h.location}" if h.location else ""
    panels = (
        f"{h.panel_qty} {h.panel_type} panels" if h.panel_qty and h.panel_type else "solar panels"
    )
    brand = f" {h.inverter_brand}" if h.inverter_brand else ""
    headline = (
        f"Your {h.system_type} system{where} has {panels} ({h.solar_capacity_kwp:.2f} kWp) "
        f"and a {h.inverter_capacity_kw:.1f} kW{brand} inverter."
    )

    details: list[str] = []
    if h.battery_present:
        battery = (
            f"Battery: {h.battery_capacity_kwh:.1f} kWh ({h.battery_usable_capacity_kwh:.1f} kWh usable) "
            f"with a {h.battery_minimum_soc_percent:.0f}% reserve"
        )
        if h.battery_backup_hours_target:
            battery += f", target backup {h.battery_backup_hours_target} hours"
        details.append(battery + ".")
    else:
        details.append("No battery is configured.")
    if h.grid_available:
        grid = f"Grid: {h.meter_type or 'standard'} meter"
        if h.discom:
            grid += f" with {h.discom}"
        if h.sanctioned_load_kw is not None:
            grid += f", sanctioned load {h.sanctioned_load_kw:.1f} kW"
        if h.tariff_rate is not None:
            grid += f", {h.tariff_type or 'flat'} tariff ₹{h.tariff_rate:.2f}/kWh"
        if h.export_credit_inr_per_kwh is not None:
            grid += f", export credit ₹{h.export_credit_inr_per_kwh:.2f}/kWh"
        details.append(grid + ".")
    else:
        details.append("Off-grid: no grid connection.")
    if h.avg_monthly_bill_inr is not None:
        details.append(f"Average monthly bill before solar: ₹{h.avg_monthly_bill_inr:.0f}.")
    details.append(f"Goal: {h.primary_goal.replace('_', ' ')}.")
    if h.appliances:
        listed = ", ".join(f"{a.name}{' (critical)' if a.critical else ''}" for a in h.appliances)
        details.append(f"Tracked appliances: {listed}.")
    return headline, " ".join(details)


def _lifetime_sentence(ctx: AIContext) -> str | None:
    live = ctx.live_energy
    if live is None or live.solar_lifetime_kwh is None:
        return None
    return f"Lifetime solar generation so far: {live.solar_lifetime_kwh:.1f} kWh."


def _home_profile_answer(ctx: AIContext) -> AnswerDraft | None:
    setup = _home_setup(ctx)
    if setup is None:
        return None
    headline, details = setup
    return AnswerDraft(
        observation=headline,
        explanation=details,
        recommendation=(
            "Ask about live solar, battery, savings or a specific appliance to get advice for this setup."
        ),
        estimated_impact=_lifetime_sentence(ctx) or "Informational only.",
    )


def _knowledge_gap_answer(ctx: AIContext) -> AnswerDraft:
    kind = ctx.knowledge.gap_kind if ctx.knowledge else None
    if kind == "fault_code":
        return AnswerDraft(
            observation="That fault code is not in the manuals uploaded for Suryaa.",
            explanation="I only quote a fault code when the reviewed inverter or battery manual contains it.",
            recommendation="Contact your installer and share the exact code on the inverter display. I will not guess.",
            estimated_impact="None.",
        )
    return AnswerDraft(
        observation="I could not find that in the documents available for your home.",
        explanation="Policies, tariffs, manuals, bills and warranties are answered from uploaded documents, not from memory.",
        recommendation="Ask a Suryaa admin to publish the document, or upload your own bill or warranty.",
        estimated_impact="None.",
    )


def _document_answer(ctx: AIContext) -> AnswerDraft | None:
    knowledge = ctx.knowledge
    if knowledge is None:
        return None
    parts: list[str] = []
    bill = knowledge.bill or {}
    if bill.get("units_kwh") is not None:
        parts.append(f"The bill records {bill['units_kwh']:.0f} kWh.")
    if bill.get("tariff_category"):
        parts.append(f"Tariff category: {bill['tariff_category']}.")
    if bill.get("sanctioned_load_kw") is not None:
        parts.append(f"Sanctioned load on the bill: {bill['sanctioned_load_kw']:.2f} kW.")
    if knowledge.passages:
        first = knowledge.passages[0]
        page = f" page {first.page}" if first.page else ""
        parts.append(f"From {first.title}{page}: {first.excerpt}")
    if not parts:
        return None
    return AnswerDraft(
        observation=parts[0],
        explanation=" ".join(parts),
        recommendation="Use the cited document for the exact wording. Live solar and battery numbers come from your energy readings, not from these documents.",
        estimated_impact="Informational only.",
    )


def compose_fallback_answer(
    *,
    reason: FallbackReason,
    intents: list[Intent],
    context: AIContext,
    analytics: AnalyticsBundle,
) -> AnswerDraft:
    if reason is FallbackReason.DEVICE_CONTROL:
        return AnswerDraft(
            observation="You asked me to change a device, but I cannot control devices yet.",
            explanation="Suryaa is in read-only mode: it analyses your energy data and does not switch appliances.",
            recommendation=(
                "Use the appliance's own switch or app. Ask “Should I run the washing machine now?” "
                "and I will tell you whether it is a good time."
            ),
            estimated_impact="None — no device was changed.",
        )
    if reason is FallbackReason.OUT_OF_SCOPE:
        return AnswerDraft(
            observation="That question is outside what I can help with.",
            explanation="I answer questions about your home's solar, battery, grid, appliances and savings.",
            recommendation="Try “What is happening in my home right now?” or “How much solar did I generate today?”.",
            estimated_impact="None.",
        )
    if reason is FallbackReason.NO_DATA:
        return AnswerDraft(
            observation="There is no energy data for your home yet.",
            explanation="Suryaa needs readings from your solar system before it can analyse anything.",
            recommendation="Check that your system or simulator is online and sending data, then ask again.",
            estimated_impact="Not enough data to estimate.",
        )
    if reason is FallbackReason.KNOWLEDGE_GAP:
        return _knowledge_gap_answer(context)

    primary = intents[0] if intents else Intent.LIVE_OVERVIEW
    no_model_note = reason in (FallbackReason.LLM_UNAVAILABLE, FallbackReason.LLM_ERROR)

    if primary is Intent.HOME_PROFILE:
        profile = _home_profile_answer(context)
        if profile is not None:
            if no_model_note:
                profile.explanation += " (Summary generated without the AI model.)"
            return profile

    if primary is Intent.DOCUMENT and context.knowledge is not None:
        drafted = _document_answer(context)
        if drafted is not None:
            if no_model_note:
                drafted.explanation += " (Summary generated without the AI model.)"
            return drafted

    observation_parts = [p for p in (_live_sentence(context), _daily_sentence(context)) if p]
    explanation_parts = [p for p in (_surplus_explanation(analytics), _top_anomaly(analytics)) if p]
    recommendation = "No action is needed right now."
    impact = "No significant impact expected."

    if primary is Intent.FULL_OVERVIEW:
        setup = _home_setup(context)
        if setup is not None:
            observation_parts.insert(0, setup[0])
            explanation_parts.insert(0, setup[1])
        surplus = analytics.solar_surplus
        if surplus is not None and surplus.state == "surplus":
            recommendation = "Run flexible appliances now to use surplus solar instead of exporting it."
        elif surplus is not None and surplus.state == "deficit":
            recommendation = "Shift flexible loads such as laundry or water heating to midday solar hours."
        if analytics.savings is not None and analytics.savings.amount_inr is not None:
            impact = f"Estimated savings ₹{analytics.savings.amount_inr:.2f} for the day."
        elif analytics.backup is not None and analytics.backup.backup_hours is not None:
            impact = f"Battery can cover about {analytics.backup.backup_hours:.1f} hours at the current load."
        else:
            impact = _lifetime_sentence(context) or impact
    elif primary is Intent.APPLIANCE_TIMING and analytics.appliance is not None:
        obs, recommendation, impact = _appliance_answer(analytics.appliance)
        observation_parts.insert(0, obs)
        explanation_parts.extend(analytics.appliance.reasons[:2])
    elif primary in (Intent.BATTERY, Intent.BACKUP) and analytics.backup is not None:
        backup = analytics.backup
        if backup.backup_hours is not None:
            observation_parts.append(
                f"About {backup.usable_energy_kwh:.2f} kWh is usable above the "
                f"{backup.reserve_soc_percent:.0f}% reserve, roughly {backup.backup_hours:.1f} hours at the current load."
            )
        explanation_parts.append(backup.note)
    elif primary is Intent.SAVINGS and analytics.savings is not None:
        savings = analytics.savings
        if savings.available and savings.amount_inr is not None:
            impact = f"Estimated savings ₹{savings.amount_inr:.2f} for the day."
        else:
            impact = savings.note or impact
    elif primary is Intent.DEVICE_USAGE and context.devices is not None:
        running = [d for d in context.devices.devices if d.current_power_kw > 0.01][:3]
        if running:
            listed = ", ".join(f"{d.name} {d.current_power_kw:.2f} kW" for d in running)
            observation_parts.insert(0, f"Top consumers right now: {listed}.")
        else:
            observation_parts.insert(0, "No tracked appliance is drawing power right now.")
    elif primary is Intent.GRID_IMPORT and analytics.solar_surplus is not None:
        if analytics.solar_surplus.state == "deficit":
            recommendation = "Shift flexible loads such as laundry or water heating to midday solar hours."
    elif primary is Intent.GRID_EXPORT and analytics.solar_surplus is not None:
        if analytics.solar_surplus.state == "surplus":
            recommendation = "Run flexible appliances now to use surplus solar instead of exporting it."
            impact = f"Up to {analytics.solar_surplus.surplus_kw:.2f} kW of solar could be used at home."

    if no_model_note:
        explanation_parts.append("(Summary generated without the AI model.)")

    return AnswerDraft(
        observation=" ".join(observation_parts) or "Live readings are not available right now.",
        explanation=" ".join(explanation_parts) or "Not enough data to explain the current state.",
        recommendation=recommendation,
        estimated_impact=impact,
    )
