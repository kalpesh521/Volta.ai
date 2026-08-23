"""
Grid import / export after solar and battery dispatch.

Optional outage window: GRID_OUTAGE_WINDOW=14:00-16:00 (local time).
Zero-export mode clips export to `zero_export_limit_kw` (default 0).
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from simulator.config import SimulatorConfig


def grid_is_available(config: SimulatorConfig, ts: datetime) -> bool:
    if not config.grid_available:
        return False
    window = (config.grid_outage_window or "").strip()
    if not window or "-" not in window:
        return True
    start_s, end_s = (part.strip() for part in window.split("-", 1))
    tz = ZoneInfo(config.timezone)
    current = ts.astimezone(tz).strftime("%H:%M")
    if start_s <= end_s:
        in_window = start_s <= current < end_s
    else:
        in_window = current >= start_s or current < end_s
    return not in_window


class GridEngine:
    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.total_import_kwh = 0.0
        self.total_export_kwh = 0.0

    def apply(
        self,
        ts: datetime,
        import_kw: float,
        export_kw: float,
        available: bool,
    ) -> dict[str, float | str]:
        import_kw = max(0.0, import_kw)
        export_kw = max(0.0, export_kw)
        if not available:
            import_kw = 0.0
        if self.config.zero_export_mode:
            export_kw = min(export_kw, max(0.0, self.config.zero_export_limit_kw))
        if import_kw > 0.0 and export_kw > 0.0:
            if import_kw >= export_kw:
                export_kw = 0.0
            else:
                import_kw = 0.0

        rng = random.Random(self.config.random_seed + int(ts.timestamp()) + 7)
        if available:
            voltage = self.config.grid_nominal_voltage_v + rng.uniform(-1.8, 1.8)
            frequency = self.config.grid_nominal_frequency_hz + rng.uniform(-0.04, 0.04)
            status = "available"
        else:
            voltage = 0.0
            frequency = 0.0
            status = "outage"

        import_kwh = import_kw * self.config.interval_hours
        export_kwh = export_kw * self.config.interval_hours
        self.total_import_kwh += import_kwh
        self.total_export_kwh += export_kwh

        return {
            "grid_status": status,
            "grid_import_power_kw": round(import_kw, 4),
            "grid_export_power_kw": round(export_kw, 4),
            "grid_import_interval_kwh": round(import_kwh, 6),
            "grid_export_interval_kwh": round(export_kwh, 6),
            "total_import_kwh": round(self.total_import_kwh, 6),
            "total_export_kwh": round(self.total_export_kwh, 6),
            "grid_voltage_v": round(voltage, 2),
            "grid_frequency_hz": round(frequency, 3),
        }
