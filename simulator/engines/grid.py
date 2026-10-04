"""
Grid availability, power quality, import / export and tariff.

Availability (any of these makes the grid unavailable):
    - GRID_AVAILABLE=false            → off-grid home, status "off_grid"
    - FORCE_GRID_OUTAGE / grid_outage scenario
    - GRID_OUTAGE_WINDOW=14:00-16:00  (local time)
    - LOAD_SHEDDING_WINDOWS=10:00-12:00,19:00-21:00 / load_shedding scenario
    - Random feeder outages: Poisson per day, more in monsoon and hot months,
      clustered at the evening peak and afternoon storms. Seeded by location,
      so neighbouring simulated homes lose power together.

Voltage follows the feeder: sags at the evening peak, rises at midday when
rooftop PV on the street exports (and rises further with this home's own
export). Above `grid_voltage_upper_limit_v` the inverter curtails export
(volt-watt), which shows up as `solar_curtailed_kw`.

Zero-export mode clips export to `zero_export_limit_kw` (default 0).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig
from simulator.engines import randomness as rnd

LOAD_SHEDDING_DEFAULT = "10:00-12:00,19:00-21:00"

# Feeder voltage offset (V) from nominal by hour of day.
_FEEDER_PROFILE_V = (
    4.0, 4.5, 5.0, 5.0, 4.5, 3.0, -1.0, -3.0, -3.0, -1.0, 2.0, 4.0,
    5.0, 5.0, 4.0, 2.0, 0.0, -4.0, -9.0, -12.0, -12.0, -10.0, -6.0, 0.0,
)


def _parse_windows(text: str) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if "-" not in chunk:
            continue
        start_s, end_s = (part.strip() for part in chunk.split("-", 1))
        try:
            sh, sm = (int(x) for x in start_s.split(":"))
            eh, em = (int(x) for x in end_s.split(":"))
        except ValueError:
            continue
        windows.append((sh * 60 + sm, eh * 60 + em))
    return windows


def _in_windows(minute: float, windows: list[tuple[int, int]]) -> bool:
    for start, end in windows:
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:
            return True
    return False


def _minute(local: datetime) -> float:
    return local.hour * 60.0 + local.minute + local.second / 60.0


@lru_cache(maxsize=2048)
def _outage_events(
    seed: int, day: date, scenario: str, rate_per_day: float, median_minutes: float
) -> tuple[tuple[float, float], ...]:
    month = day.month
    rate = rate_per_day * (2.0 if month in (6, 7, 8, 9) else 1.5 if month in (4, 5) else 1.0)
    if scenario == "monsoon_storm":
        rate *= 4.0
    # Inverse-CDF Poisson draw.
    u = rnd.uniform(seed, "outage-count", day)
    count, p, cumulative = 0, math.exp(-rate), math.exp(-rate)
    while u > cumulative and count < 8:
        count += 1
        p *= rate / count
        cumulative += p
    weights = {str(h): (2.0 if 18 <= h <= 22 else 1.5 if 13 <= h <= 17 else 1.0) for h in range(24)}
    events: list[tuple[float, float]] = []
    for i in range(count):
        hour = int(rnd.weighted_choice(weights, seed, "outage-hour", day, i))
        start = hour * 60.0 + rnd.uniform(seed, "outage-min", day, i) * 60.0
        duration = median_minutes * math.exp(0.9 * rnd.normal(seed, "outage-dur", day, i))
        events.append((start, start + min(240.0, max(2.0, duration))))
    if scenario == "monsoon_storm" and rnd.chance(0.7, seed, "storm-outage", day):
        start = 15.0 * 60 + rnd.uniform(seed, "storm-outage-start", day) * 90.0
        events.append((start, start + rnd.uniform_between(20.0, 150.0, seed, "storm-outage-dur", day)))
    return tuple(events)


def random_outage_active(config: SimulatorConfig, local: datetime, scenario: str) -> bool:
    if not config.random_grid_outages or config.grid_outage_rate_per_day <= 0:
        return False
    minute = _minute(local)
    seed = config.location_seed
    today = local.date()
    for start, end in _outage_events(seed, today, scenario, config.grid_outage_rate_per_day, config.grid_outage_median_minutes):
        if start <= minute < end:
            return True
    yesterday = today - timedelta(days=1)
    for start, end in _outage_events(seed, yesterday, scenario, config.grid_outage_rate_per_day, config.grid_outage_median_minutes):
        if end > 1440.0 and minute < end - 1440.0:
            return True
    return False


def grid_outage_cause(config: SimulatorConfig, ts: datetime, scenario: str | None = None) -> str | None:
    """None when the grid is up, else why it is down."""
    scenario = scenario or config.scenario
    if not config.grid_available:
        return "off_grid"
    if config.force_grid_outage or scenario == "grid_outage":
        return "grid_outage"
    local = ts.astimezone(ZoneInfo(config.timezone))
    minute = _minute(local)
    if _in_windows(minute, _parse_windows(config.grid_outage_window)):
        return "scheduled_outage"
    shedding = config.load_shedding_windows or (LOAD_SHEDDING_DEFAULT if scenario == "load_shedding" else "")
    if _in_windows(minute, _parse_windows(shedding)):
        return "load_shedding"
    if random_outage_active(config, local, scenario):
        return "feeder_fault"
    return None


def grid_is_available(config: SimulatorConfig, ts: datetime, scenario: str | None = None) -> bool:
    return grid_outage_cause(config, ts, scenario) is None


def feeder_voltage_v(config: SimulatorConfig, ts: datetime, scenario: str | None = None) -> float:
    """Street voltage before this home's own import/export."""
    scenario = scenario or config.scenario
    local = ts.astimezone(ZoneInfo(config.timezone))
    hours = local.hour + local.minute / 60.0
    i = int(hours) % 24
    frac = hours - int(hours)
    offset = _FEEDER_PROFILE_V[i] + (_FEEDER_PROFILE_V[(i + 1) % 24] - _FEEDER_PROFILE_V[i]) * frac
    if local.month in (4, 5, 6) and 18 <= local.hour <= 23:
        offset -= 5.0
    if scenario == "voltage_rise" and 10 <= local.hour < 16:
        offset += 16.0
    t = local.timestamp()
    noise = 6.0 * (rnd.smooth_noise(t, 120.0, config.location_seed, "feeder-v") - 0.5)
    noise += 2.0 * (rnd.smooth_noise(t, 20.0, config.household_seed, "service-v") - 0.5)
    return config.grid_nominal_voltage_v + offset + noise


def export_limit_kw(config: SimulatorConfig, feeder_v: float) -> float | None:
    """Volt-watt: export the inverter may push before hitting the upper limit."""
    k = max(1e-3, config.grid_feeder_v_per_kw)
    headroom = config.grid_voltage_upper_limit_v - feeder_v
    if headroom >= 10.0 * k:
        return None
    return max(0.0, headroom / k)


def tariff_period(config: SimulatorConfig, ts: datetime) -> tuple[str, float | None]:
    """(period label, ₹/kWh import rate) for this tick."""
    base = config.tariff_rate_inr_per_kwh
    if base is None:
        return "unknown", None
    kind = (config.tariff_type or "").lower()
    if "time" not in kind:
        return ("slab" if "slab" in kind else "flat"), base
    local = ts.astimezone(ZoneInfo(config.timezone))
    minute = _minute(local)
    if _in_windows(minute, _parse_windows(config.tou_peak_windows)):
        return "peak", round(base * config.tou_peak_multiplier, 3)
    if _in_windows(minute, _parse_windows(config.tou_offpeak_windows)):
        return "off_peak", round(base * config.tou_offpeak_multiplier, 3)
    return "normal", base


class GridEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.total_import_kwh = 0.0
        self.total_export_kwh = 0.0
        self.import_today_kwh = 0.0
        self.export_today_kwh = 0.0
        self.import_cost_today_inr = 0.0
        self.export_credit_today_inr = 0.0
        self.outage_minutes_today = 0.0
        self._today: date | None = None

    def _reset_daily(self, local: datetime) -> None:
        if self._today != local.date():
            self._today = local.date()
            self.import_today_kwh = 0.0
            self.export_today_kwh = 0.0
            self.import_cost_today_inr = 0.0
            self.export_credit_today_inr = 0.0
            self.outage_minutes_today = 0.0

    def apply(
        self,
        ts: datetime,
        import_kw: float,
        export_kw: float,
        available: bool,
        scenario: str | None = None,
        feeder_v: float | None = None,
        outage_cause: str | None = None,
    ) -> dict[str, Any]:
        cfg = self.config
        scenario = scenario or cfg.scenario
        local = ts.astimezone(ZoneInfo(cfg.timezone))
        self._reset_daily(local)
        import_kw = max(0.0, import_kw)
        export_kw = max(0.0, export_kw)
        if not available:
            import_kw = 0.0
            export_kw = 0.0
        if cfg.zero_export_mode:
            export_kw = min(export_kw, max(0.0, cfg.zero_export_limit_kw))
        if import_kw > 0.0 and export_kw > 0.0:
            if import_kw >= export_kw:
                export_kw = 0.0
            else:
                import_kw = 0.0

        if available:
            base_v = feeder_v if feeder_v is not None else feeder_voltage_v(cfg, local, scenario)
            k = cfg.grid_feeder_v_per_kw
            voltage = base_v + k * export_kw - 0.4 * k * import_kw
            t = local.timestamp()
            frequency = (
                cfg.grid_nominal_frequency_hz
                + 0.08 * (rnd.smooth_noise(t, 600.0, cfg.location_seed, "freq") - 0.5)
                + 0.02 * (rnd.smooth_noise(t, 30.0, cfg.location_seed, "freq-fast") - 0.5)
                - (0.03 if 18 <= local.hour <= 22 else 0.0)
            )
            status = "available"
        else:
            voltage = 0.0
            frequency = 0.0
            status = "off_grid" if not cfg.grid_available else "outage"
            self.outage_minutes_today += cfg.simulation_interval_seconds / 60.0 if cfg.grid_available else 0.0

        period, rate = tariff_period(cfg, local)
        export_credit = cfg.export_credit_inr_per_kwh
        if (cfg.meter_type or "").lower() == "no export" or cfg.zero_export_mode:
            export_credit = 0.0 if export_credit is not None else None

        import_kwh = import_kw * cfg.interval_hours
        export_kwh = export_kw * cfg.interval_hours
        self.total_import_kwh += import_kwh
        self.total_export_kwh += export_kwh
        self.import_today_kwh += import_kwh
        self.export_today_kwh += export_kwh
        import_cost = import_kwh * rate if rate is not None else None
        export_value = export_kwh * export_credit if export_credit is not None else None
        if import_cost is not None:
            self.import_cost_today_inr += import_cost
        if export_value is not None:
            self.export_credit_today_inr += export_value

        return {
            "grid_status": status,
            "grid_outage_cause": outage_cause if not available else None,
            "grid_import_power_kw": round(import_kw, 4),
            "grid_export_power_kw": round(export_kw, 4),
            "grid_import_interval_kwh": round(import_kwh, 6),
            "grid_export_interval_kwh": round(export_kwh, 6),
            "grid_import_today_kwh": round(self.import_today_kwh, 4),
            "grid_export_today_kwh": round(self.export_today_kwh, 4),
            "total_import_kwh": round(self.total_import_kwh, 6),
            "total_export_kwh": round(self.total_export_kwh, 6),
            "grid_voltage_v": round(voltage, 2),
            "grid_frequency_hz": round(frequency, 3),
            "grid_outage_minutes_today": round(self.outage_minutes_today, 1),
            "tariff_period": period,
            "tariff_rate_inr_per_kwh": rate,
            "export_credit_inr_per_kwh": export_credit,
            "grid_import_cost_interval_inr": round(import_cost, 5) if import_cost is not None else None,
            "grid_export_credit_interval_inr": round(export_value, 5) if export_value is not None else None,
            "grid_import_cost_today_inr": round(self.import_cost_today_inr, 3) if rate is not None else None,
            "grid_export_credit_today_inr": round(self.export_credit_today_inr, 3) if export_credit is not None else None,
        }

    def state(self) -> dict[str, Any]:
        return {
            "total_import_kwh": self.total_import_kwh,
            "total_export_kwh": self.total_export_kwh,
            "import_today_kwh": self.import_today_kwh,
            "export_today_kwh": self.export_today_kwh,
            "import_cost_today_inr": self.import_cost_today_inr,
            "export_credit_today_inr": self.export_credit_today_inr,
            "outage_minutes_today": self.outage_minutes_today,
            "today": self._today.isoformat() if self._today else None,
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.total_import_kwh = float(data.get("total_import_kwh", 0.0))
        self.total_export_kwh = float(data.get("total_export_kwh", 0.0))
        self.import_today_kwh = float(data.get("import_today_kwh", 0.0))
        self.export_today_kwh = float(data.get("export_today_kwh", 0.0))
        self.import_cost_today_inr = float(data.get("import_cost_today_inr", 0.0))
        self.export_credit_today_inr = float(data.get("export_credit_today_inr", 0.0))
        self.outage_minutes_today = float(data.get("outage_minutes_today", 0.0))
        self._today = date.fromisoformat(data["today"]) if data.get("today") else None
