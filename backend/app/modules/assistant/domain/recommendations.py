"""Deterministic recommendations. The language model does not calculate these."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HourPoint:
    hour: str
    solar_kwh: float
    home_kwh: float
    import_kwh: float


@dataclass(frozen=True)
class DeviceOption:
    device: str
    rated_power_kw: float
    controllable: bool
    critical: bool
    state: str


_PREFERENCE = ("water_heater", "washing_machine", "water_pump", "air_conditioner", "ev_charger")


def identify_solar_surplus_window(hours: list[HourPoint]) -> dict | None:
    best: list[HourPoint] = []
    current: list[HourPoint] = []
    for point in hours:
        if point.solar_kwh > point.home_kwh + 0.05:
            current.append(point)
            if len(current) > len(best):
                best = list(current)
        else:
            current = []
    if not best:
        return None
    return {
        "start_hour": best[0].hour,
        "end_hour": best[-1].hour,
        "hours": len(best),
        "surplus_kwh": round(sum(item.solar_kwh - item.home_kwh for item in best), 3),
    }


def detect_high_grid_import(import_kw: float, *, threshold_kw: float = 1.0) -> bool:
    return import_kw > threshold_kw


def detect_abnormal_consumption(hour_kwh: list[float]) -> bool:
    if len(hour_kwh) < 4:
        return False
    earlier = sorted(hour_kwh[:-1])
    typical = earlier[len(earlier) // 2]
    return hour_kwh[-1] > max(0.4, typical * 1.8)


def find_best_device_schedule(
    devices: list[DeviceOption],
    *,
    surplus_kw: float,
    soc_percent: float | None,
    reserve_percent: float,
) -> dict | None:
    if surplus_kw <= 0.2:
        return None
    if soc_percent is not None and soc_percent <= reserve_percent:
        return None
    ranked = sorted(
        devices,
        key=lambda item: _PREFERENCE.index(item.device) if item.device in _PREFERENCE else len(_PREFERENCE),
    )
    for device in ranked:
        if device.critical or not device.controllable:
            continue
        if device.state == "on":
            continue
        if device.rated_power_kw > surplus_kw + 0.05:
            continue
        if device.device not in _PREFERENCE:
            continue
        return {
            "device": device.device,
            "command": "on",
            "duration_minutes": 30,
            "surplus_kw": round(surplus_kw, 3),
            "reason": "Solar surplus covers this load and the battery is above the reserve.",
        }
    return None


def estimate_backup_hours(*, usable_kwh: float | None, load_kw: float | None) -> float | None:
    if usable_kwh is None or load_kw is None or load_kw <= 0.05:
        return None
    return round(max(0.0, usable_kwh) / load_kw, 1)
