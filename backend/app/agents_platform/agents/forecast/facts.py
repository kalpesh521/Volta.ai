"""
Facts for the answer model.

Every number the answer model states is checked against these facts by the grounding guard, so
anything a user might ask ("when is the best time to run the washing machine?", "will I have
surplus?", "how much will I generate tomorrow?") is derived HERE, rounded, and labelled. The model
is never expected to add up slots. kW are rounded to 2 decimals, kWh to 1; times are local ISO
(`2026-11-15T12:00+05:30`).

Windows are defined on the first 24 h of the horizon so "today / tonight / next 24 h" mean the same
thing whatever the horizon requested.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .result import ForecastSlot

if TYPE_CHECKING:
    from .result import ForecastResult

SURPLUS_THRESHOLD_KW = 0.1
EVENING_HOURS = range(18, 23)  # slots starting 18:00 .. 22:00 local
_TOMORROW_DAYTIME_END_HOUR = 19


def _kw(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _kwh(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _iso(slot: ForecastSlot) -> str:
    return slot.start.isoformat(timespec="minutes")


def _surplus_window(slots: list[ForecastSlot]) -> dict[str, Any] | None:
    """Contiguous run of hours where forecast solar exceeds load, choosing the run with most energy."""
    best: list[ForecastSlot] = []
    best_kwh = 0.0
    run: list[ForecastSlot] = []

    def close() -> None:
        nonlocal best, best_kwh, run
        kwh = sum(s.net_kw_p50 or 0.0 for s in run)
        if run and kwh > best_kwh:
            best, best_kwh = run, kwh
        run = []

    for s in slots:
        net = s.net_kw_p50
        if net is not None and net > SURPLUS_THRESHOLD_KW:
            run.append(s)
        else:
            close()
    close()
    if not best:
        return None
    return {
        "start": _iso(best[0]),
        "end": (best[-1].start + timedelta(hours=1)).isoformat(timespec="minutes"),
        "surplus_kwh": _kwh(best_kwh),
        "peak_surplus_kw": _kw(max(s.net_kw_p50 or 0.0 for s in best)),
    }


def _evening_deficit(slots: list[ForecastSlot]) -> dict[str, Any] | None:
    evening = [s for s in slots if s.start.hour in EVENING_HOURS and s.load_kw_p50 is not None]
    if not evening:
        return None
    first_day = evening[0].start.date()
    day = [s for s in evening if s.start.date() == first_day]
    deficit = sum(max(0.0, (s.load_kw_p50 or 0.0) - s.solar_kw_p50) for s in day)
    return {
        "date": first_day.isoformat(),
        "window": "18:00-23:00 local",
        "hours_covered": len(day),
        "deficit_kwh": _kwh(deficit),
    }


def build_facts(result: "ForecastResult", *, include_hourly: bool = True) -> dict[str, Any]:
    slots = result.slots
    first_day = slots[:24]
    now_local = result.generated_at.astimezone(slots[0].start.tzinfo) if slots else result.generated_at
    today = now_local.date()

    facts: dict[str, Any] = {
        "generated_at": now_local.isoformat(timespec="minutes"),
        "timezone": result.timezone,
        "horizon_hours": result.horizon_hours,
        "confidence": {"level": result.confidence.level, "reasons": list(result.confidence.reasons)},
        "methods": {
            "solar": result.methods.solar,
            "load": result.methods.load,
            "weather": result.methods.weather,
            "weather_kind": result.methods.weather_kind,
        },
        "performance_ratio_used": round(result.performance_ratio, 3),
        "calibration_samples": result.data_points_used.solar_calibration,
        "interval_basis": dict(result.interval_basis),
        "warnings": list(result.warnings),
    }
    if not slots:
        return facts

    peak = max(first_day, key=lambda s: s.solar_kw_p50)
    facts["peak_solar"] = {"hour_start": _iso(peak), "kw": _kw(peak.solar_kw_p50), "kw_p10": _kw(peak.solar_kw_p10), "kw_p90": _kw(peak.solar_kw_p90)}
    facts["solar_kwh_next_24h"] = _kwh(sum(s.solar_kw_p50 for s in first_day))
    facts["solar_kwh_next_24h_range"] = {
        "low": _kwh(sum(s.solar_kw_p10 for s in first_day)),
        "high": _kwh(sum(s.solar_kw_p90 for s in first_day)),
    }
    facts["solar_kwh_remaining_today"] = _kwh(sum(s.solar_kw_p50 for s in slots if s.start.date() == today))

    tomorrow = [s for s in slots if (s.start.date() - today).days == 1]
    if tomorrow and tomorrow[-1].start.hour >= _TOMORROW_DAYTIME_END_HOUR and (tomorrow[0].start.hour <= 5):
        facts["solar_kwh_tomorrow"] = _kwh(sum(s.solar_kw_p50 for s in tomorrow))

    if result.totals.load_kwh is not None:
        load_24 = sum(s.load_kw_p50 or 0.0 for s in first_day)
        facts["load_kwh_next_24h"] = _kwh(load_24)
        facts["net_kwh_next_24h"] = _kwh(sum(s.solar_kw_p50 for s in first_day) - load_24)
        facts["surplus_window"] = _surplus_window(first_day)
        facts["evening_deficit"] = _evening_deficit(first_day)
    else:
        facts["load_forecast"] = "unavailable (not enough load history)"

    if include_hourly:
        hourly = []
        for s in slots:
            row: dict[str, Any] = {
                "t": _iso(s),
                "solar_kw": _kw(s.solar_kw_p50),
                "solar_kw_p10": _kw(s.solar_kw_p10),
                "solar_kw_p90": _kw(s.solar_kw_p90),
                "cloud_pct": round(s.cloud_cover_pct),
            }
            if s.load_kw_p50 is not None:
                row["load_kw"] = _kw(s.load_kw_p50)
            hourly.append(row)
        facts["hourly"] = hourly
    return facts
