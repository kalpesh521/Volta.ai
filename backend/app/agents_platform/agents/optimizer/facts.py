"""
Facts for the answer model.

Every number the answer model states is checked against these facts by the grounding guard, so anything a
good answer would quote ("when should I run the washing machine?", "how much do I save?", "when does the
battery charge?") is computed HERE, rounded and labelled. The model is never expected to add up slots.

Rounding: kW 2 decimals, kWh 1, INR 2, percent 1. The human-readable `plan_summary` strings round INR to
whole rupees, which is a rounding of the numeric fields next to them, so the guard accepts both. Times are
LOCAL wall-clock labels ("11:00") taken straight from the slot start times; a slot after local midnight is
suffixed "tomorrow". Windows are `"HH:MM-HH:MM"` with the end being the end of the last slot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .models import Status

if TYPE_CHECKING:
    from .models import ApplianceSchedule, OptimizationResult, SlotPlan

_MAX_WINDOWS = 6
_MIN_WINDOW_KWH = 0.05


def _kw(x: float) -> float:
    return round(float(x), 2)


def _kwh(x: float) -> float:
    return round(float(x), 1)


def _inr(x: float) -> float:
    return round(float(x), 2)


def _pct(x: float) -> float:
    return round(float(x), 1)


def build_facts(result: OptimizationResult) -> dict[str, Any]:
    if result.status is Status.INFEASIBLE or result.totals is None or result.baseline is None:
        return {
            "status": Status.INFEASIBLE.value,
            "goal": result.goal.value,
            "problems": [v.message for v in result.violations[:3]],
            "violation_count": len(result.violations),
            "warnings": list(result.warnings),
        }

    slots = result.slots
    first_day = slots[0].start.date()
    step = timedelta(minutes=result.slot_minutes)

    def label(moment: datetime) -> str:
        text = moment.strftime("%H:%M")
        return text if moment.date() == first_day else f"{text} tomorrow"

    def window(i0: int, i1: int) -> str:
        return f"{label(slots[i0].start)}-{(slots[i1].start + step).strftime('%H:%M')}"

    totals, base = result.totals, result.baseline
    appliances = [_appliance_fact(a, label, result.slot_minutes) for a in result.appliances]
    battery = _battery_facts(result, label, window)
    prices = _price_facts(slots, label)

    summary: list[str] = [a["summary"] for a in appliances]
    summary.extend(battery["summary"] if battery else [])

    facts: dict[str, Any] = {
        "status": result.status.value,
        "goal": result.goal.value,
        "horizon_hours": _kwh(len(slots) * result.slot_minutes / 60.0),
        "slot_minutes": result.slot_minutes,
        "plan_cost_inr": _inr(totals.cost_inr),
        "baseline_cost_inr": _inr(base.cost_inr),
        "savings_inr": _inr(result.savings_inr),
        "savings_percent": _pct(result.savings_percent),
        "grid_import_kwh": _kwh(totals.grid_import_kwh),
        "baseline_grid_import_kwh": _kwh(base.grid_import_kwh),
        "import_reduction_kwh": _kwh(result.savings_kwh),
        "grid_export_kwh": _kwh(totals.grid_export_kwh),
        "baseline_grid_export_kwh": _kwh(base.grid_export_kwh),
        "solar_generated_kwh": _kwh(totals.solar_kwh),
        "self_consumption_percent": _pct(totals.self_consumption_percent),
        "baseline_self_consumption_percent": _pct(base.self_consumption_percent),
        "self_sufficiency_percent": _pct(totals.self_sufficiency_percent),
        "load_kwh": _kwh(totals.load_kwh),
        "unserved_kwh": _kwh(totals.unserved_kwh),
        "curtailed_solar_kwh": _kwh(totals.curtailed_kwh),
        "appliances": appliances,
        "price": prices,
        "outage_windows": _runs(slots, lambda s: not s.grid_available, window),
        "unserved_windows": _runs(slots, lambda s: s.unserved_kw > 1e-6, window),
        "plan_summary": summary,
        "violation_count": 0,
        "warnings": list(result.warnings),
    }
    if battery:
        facts["battery"] = battery
        facts["final_battery_soc_percent"] = _pct(totals.final_soc_percent or 0.0)
        facts["baseline_final_battery_soc_percent"] = _pct(base.final_soc_percent or 0.0)
    return facts


def _appliance_fact(a: ApplianceSchedule, label, slot_minutes: int) -> dict[str, Any]:
    pref = label(a.preferred_start)
    duration_h = a.duration_slots * slot_minutes / 60.0
    fact: dict[str, Any] = {
        "name": a.name,
        "rated_kw": _kw(a.rated_kw),
        "duration_hours": _kwh(duration_h),
        "energy_kwh": _kwh(a.rated_kw * duration_h),
        "critical": a.critical,
        "placed": a.placed,
        "preferred_start": pref,
        "preferred_time_feasible": a.preferred_feasible,
    }
    if not a.placed or a.start is None or a.end is None:
        fact["summary"] = f"{a.name} could not be scheduled"
        return fact
    start = label(a.start)
    fact.update(start=start, end=a.end.strftime("%H:%M"), shifted=a.shifted)
    if a.critical:
        fact["summary"] = f"{a.name} stays at {start} (critical load, never shifted)"
    elif a.shifted and a.saves_inr is not None and a.saves_inr >= 0.5:
        fact["saves_inr"] = _inr(a.saves_inr)
        fact["summary"] = f"run {a.name} at {start}, saves {round(a.saves_inr)} INR vs {pref}"
    elif a.shifted:
        fact["summary"] = f"run {a.name} at {start} instead of {pref} (preferred time is not possible)"
    else:
        fact["saves_inr"] = 0.0
        fact["summary"] = f"run {a.name} at {start} (already the best time)"
    return fact


def _battery_facts(result: OptimizationResult, label, window) -> dict[str, Any] | None:
    assert result.totals is not None
    if result.totals.final_soc_percent is None:
        return None
    slots = result.slots
    step_h = result.slot_minutes / 60.0
    charge = _action_windows(slots, "charge", step_h, window)
    discharge = _action_windows(slots, "discharge", step_h, window)
    peak = max(slots, key=lambda s: s.soc_percent)
    low = min(slots, key=lambda s: s.soc_percent)
    cap = result.meta.get("battery_capacity_kwh") or 0.0
    reserve = result.meta.get("battery_reserve_kwh") or 0.0
    grid_charge_kwh = sum(
        max(0.0, s.battery_charge_kw - max(0.0, s.solar_used_kw - (s.load_kw - s.unserved_kw))) * step_h for s in slots
    )
    summary = [f"charge battery {w['window']}" for w in charge] + [f"discharge battery {w['window']}" for w in discharge]
    return {
        "charge_windows": charge,
        "discharge_windows": discharge,
        "charged_kwh": _kwh(result.totals.battery_charged_kwh),
        "discharged_kwh": _kwh(result.totals.battery_discharged_kwh),
        "charged_from_grid_kwh": _kwh(grid_charge_kwh),
        "peak_soc_percent": _pct(peak.soc_percent),
        "peak_soc_time": label(peak.start),
        "lowest_soc_percent": _pct(low.soc_percent),
        "reserve_soc_percent": _pct(reserve / cap * 100.0) if cap > 0 else None,
        "capacity_kwh": _kwh(cap),
        "summary": summary,
    }


def _action_windows(slots: tuple[SlotPlan, ...], action: str, step_h: float, window) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(slots):
        if slots[i].action != action:
            i += 1
            continue
        j = i
        while j + 1 < len(slots) and slots[j + 1].action == action:
            j += 1
        run = slots[i : j + 1]
        kw = [s.battery_charge_kw if action == "charge" else s.battery_discharge_kw for s in run]
        kwh = sum(kw) * step_h
        if kwh >= _MIN_WINDOW_KWH:
            out.append({"window": window(i, j), "kwh": _kwh(kwh), "avg_kw": _kw(sum(kw) / len(kw)), "peak_kw": _kw(max(kw))})
        i = j + 1
    if len(out) > _MAX_WINDOWS:
        biggest = sorted(out, key=lambda w: -w["kwh"])[:_MAX_WINDOWS]
        out = [w for w in out if w in biggest]
    return out


def _price_facts(slots: tuple[SlotPlan, ...], label) -> dict[str, Any]:
    cheapest = min(slots, key=lambda s: (s.import_price_inr_per_kwh, s.index))
    dearest = max(slots, key=lambda s: (s.import_price_inr_per_kwh, -s.index))
    return {
        "min_import_inr_per_kwh": _inr(cheapest.import_price_inr_per_kwh),
        "cheapest_time": label(cheapest.start),
        "max_import_inr_per_kwh": _inr(dearest.import_price_inr_per_kwh),
        "peak_time": label(dearest.start),
    }


def _runs(slots: tuple[SlotPlan, ...], predicate, window) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(slots):
        if not predicate(slots[i]):
            i += 1
            continue
        j = i
        while j + 1 < len(slots) and predicate(slots[j + 1]):
            j += 1
        out.append(window(i, j))
        i = j + 1
    return out
