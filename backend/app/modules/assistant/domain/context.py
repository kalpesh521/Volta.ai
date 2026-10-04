"""
Compact, LLM-facing views of the energy DTOs.

Tools return these instead of raw API payloads: rounded numbers, no totals
counters, no zero flows, devices sorted by draw, only non-empty hourly
buckets. This keeps prompts small and removes fields the model could misread.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.modules.energy.schemas import (
    BatteryStatusOut,
    DailySummaryOut,
    DevicesOut,
    GridStatusOut,
    HourlySummaryOut,
    LiveEnergyOut,
    SimulatorProfileOut,
    TelemetryRecord,
    WeatherOut,
)

_OFF_STATES = frozenset({"off", "idle", "standby"})

# WMO weather interpretation codes used by Open-Meteo.
_WMO_CONDITIONS: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


def _r(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


class ConfiguredAppliance(BaseModel):
    name: str
    type: str
    rated_power_kw: float
    priority: str
    critical: bool
    controllable: bool


class OnboardingDetails(BaseModel):
    """User-entered onboarding fields that the simulator profile does not carry."""

    panel_type: str | None = None
    panel_qty: int | None = None
    inverter_brand: str | None = None
    avg_monthly_bill_inr: float | None = None
    battery_backup_hours_target: int | None = None
    sanctioned_load_kw: float | None = None
    discom: str | None = None


class HouseholdFacts(BaseModel):
    system_type: str
    location: str | None = None
    panel_type: str | None = None
    panel_qty: int | None = None
    solar_capacity_kwp: float
    inverter_brand: str | None = None
    inverter_capacity_kw: float
    battery_present: bool
    battery_capacity_kwh: float
    battery_usable_capacity_kwh: float
    battery_minimum_soc_percent: float
    battery_backup_hours_target: int | None = None
    battery_max_charge_kw: float | None = None
    battery_max_discharge_kw: float | None = None
    grid_available: bool
    zero_export_mode: bool
    meter_type: str | None = None
    sanctioned_load_kw: float | None = None
    discom: str | None = None
    tariff_type: str | None = None
    tariff_rate: float | None = None
    export_credit_inr_per_kwh: float | None = None
    avg_monthly_bill_inr: float | None = None
    primary_goal: str
    appliances: list[ConfiguredAppliance] = Field(default_factory=list)


class LiveFacts(BaseModel):
    timestamp: datetime
    data_source: str
    data_quality: str
    location: str | None = None
    timezone: str | None = None
    solar_kw: float
    load_kw: float
    flows_kw: dict[str, float] = Field(default_factory=dict)
    energy_balance_status: str
    energy_balance_error_kw: float | None = None
    solar_today_kwh: float
    consumption_today_kwh: float
    solar_lifetime_kwh: float | None = None
    consumption_lifetime_kwh: float | None = None
    scenario: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BatteryFacts(BaseModel):
    timestamp: datetime | None = None
    soc_percent: float
    soh_percent: float
    status: str
    charge_kw: float
    discharge_kw: float
    energy_available_kwh: float
    temperature_c: float | None = None
    fault_code: str | None = None


class GridFacts(BaseModel):
    timestamp: datetime | None = None
    status: str
    import_kw: float
    export_kw: float
    voltage_v: float | None = None
    frequency_hz: float | None = None
    lifetime_import_kwh: float | None = None
    lifetime_export_kwh: float | None = None


class DeviceFacts(BaseModel):
    device_id: str
    name: str
    type: str
    state: str
    current_power_kw: float
    rated_power_kw: float
    priority: str
    critical: bool
    controllable: bool

    @property
    def is_on(self) -> bool:
        return self.state.lower() not in _OFF_STATES and self.current_power_kw > 0.01


class DevicesFacts(BaseModel):
    timestamp: datetime
    total_device_power_kw: float
    devices: list[DeviceFacts]


class WeatherFacts(BaseModel):
    timestamp: datetime
    condition: str | None = None
    temperature_c: float
    humidity_percent: float | None = None
    cloud_cover_percent: float
    precipitation_mm: float | None = None
    precipitation_probability_percent: float
    wind_speed_kmh: float | None = None
    shortwave_radiation_wm2: float
    direct_radiation_wm2: float | None = None
    diffuse_radiation_wm2: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    source: str | None = None
    data_quality: str


class DailyFacts(BaseModel):
    date: str
    timezone: str
    reading_count: int
    period_end: datetime | None = None
    solar_generation_kwh: float
    home_consumption_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_charge_kwh: float
    battery_discharge_kwh: float
    peak_load_kw: float
    estimated_savings_inr: float | None = None
    savings_method: str | None = None
    tariff_rate: float | None = None
    export_credit_inr_per_kwh: float | None = None


class TrendFacts(BaseModel):
    """How the home changed over the recent window (from tick history)."""

    window_minutes: int
    samples: int
    solar_kw_start: float
    solar_kw_now: float
    solar_kw_max: float
    solar_direction: str
    load_kw_avg: float
    load_kw_max: float
    battery_soc_start_percent: float | None = None
    battery_soc_now_percent: float | None = None
    battery_soc_change_percent: float | None = None
    grid_import_kwh: float
    grid_export_kwh: float


class HourFacts(BaseModel):
    hour: str
    solar_kwh: float
    home_kwh: float
    import_kwh: float
    export_kwh: float


class HourlyFacts(BaseModel):
    date: str
    timezone: str
    hours: list[HourFacts]
    peak_solar_hour: str | None = None
    peak_consumption_hour: str | None = None
    peak_import_hour: str | None = None


class KnowledgePassageFacts(BaseModel):
    document_id: str
    title: str
    doc_type: str
    page: int | None = None
    excerpt: str
    score: float


class KnowledgeFacts(BaseModel):
    passages: list[KnowledgePassageFacts] = Field(default_factory=list)
    bill: dict[str, Any] | None = None
    gap_kind: str | None = None


class AIContext(BaseModel):
    """The single structured object the answer LLM receives."""

    household: HouseholdFacts | None = None
    live_energy: LiveFacts | None = None
    battery: BatteryFacts | None = None
    grid: GridFacts | None = None
    devices: DevicesFacts | None = None
    weather: WeatherFacts | None = None
    today_summary: DailyFacts | None = None
    hourly_summary: HourlyFacts | None = None
    recent_trend: TrendFacts | None = None
    knowledge: KnowledgeFacts | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @property
    def data_time(self) -> datetime | None:
        for stamp in (
            self.live_energy.timestamp if self.live_energy else None,
            self.battery.timestamp if self.battery else None,
            self.grid.timestamp if self.grid else None,
            self.devices.timestamp if self.devices else None,
            self.weather.timestamp if self.weather else None,
            self.today_summary.period_end if self.today_summary else None,
        ):
            if stamp is not None:
                return stamp
        return None

    @property
    def data_quality(self) -> str | None:
        return self.live_energy.data_quality if self.live_energy else None


def household_facts(
    profile: SimulatorProfileOut, details: OnboardingDetails | None = None
) -> HouseholdFacts:
    details = details or OnboardingDetails()
    battery = profile.battery_present
    return HouseholdFacts(
        system_type=profile.system_type,
        location=profile.location,
        panel_type=details.panel_type or profile.panel_type,
        panel_qty=details.panel_qty or profile.panel_qty,
        solar_capacity_kwp=_r(profile.solar_capacity_kwp, 3) or 0.0,
        inverter_brand=details.inverter_brand,
        inverter_capacity_kw=_r(profile.inverter_capacity_kw) or 0.0,
        battery_present=battery,
        battery_capacity_kwh=_r(profile.battery_capacity_kwh) or 0.0,
        battery_usable_capacity_kwh=_r(profile.battery_usable_capacity_kwh) or 0.0,
        battery_minimum_soc_percent=_r(profile.battery_minimum_soc_percent, 1) or 0.0,
        battery_backup_hours_target=details.battery_backup_hours_target if battery else None,
        battery_max_charge_kw=_r(profile.battery_max_charge_power_kw) if battery else None,
        battery_max_discharge_kw=_r(profile.battery_max_discharge_power_kw) if battery else None,
        grid_available=profile.grid_available,
        zero_export_mode=profile.zero_export_mode,
        meter_type=profile.meter_type,
        sanctioned_load_kw=_r(details.sanctioned_load_kw),
        discom=details.discom,
        tariff_type=profile.tariff_type,
        tariff_rate=_r(profile.tariff_rate),
        export_credit_inr_per_kwh=_r(profile.export_credit_inr_per_kwh),
        avg_monthly_bill_inr=_r(details.avg_monthly_bill_inr),
        primary_goal=profile.primary_goal,
        appliances=[
            ConfiguredAppliance(
                name=spec.device_name,
                type=spec.device_type,
                rated_power_kw=_r(spec.rated_power_kw) or 0.0,
                priority=spec.device_priority,
                critical=spec.critical,
                controllable=spec.controllable,
            )
            for spec in profile.devices
        ],
    )


def live_facts(live: LiveEnergyOut) -> LiveFacts:
    flows = {
        name: _r(value, 3)
        for name, value in live.flows.model_dump().items()
        if isinstance(value, (int, float)) and value > 0.005
    }
    place = live.location
    balanced = live.energy_balance_status == "valid"
    return LiveFacts(
        timestamp=live.timestamp,
        data_source=live.data_source,
        data_quality=live.data_quality,
        location=(place.label or place.name) if place else None,
        timezone=place.timezone if place else None,
        solar_kw=_r(live.solar.power_kw, 3) or 0.0,
        load_kw=_r(live.load.power_kw, 3) or 0.0,
        flows_kw=flows,
        energy_balance_status=live.energy_balance_status,
        energy_balance_error_kw=None if balanced else _r(live.energy_balance_error_kw, 3),
        solar_today_kwh=_r(live.solar.energy_today_kwh, 3) or 0.0,
        consumption_today_kwh=_r(live.load.consumption_today_kwh, 3) or 0.0,
        solar_lifetime_kwh=_r(live.solar.energy_total_kwh, 1),
        consumption_lifetime_kwh=_r(live.load.consumption_total_kwh, 1),
        scenario=live.scenario,
        warnings=live.warnings[:5],
    )


def battery_facts(battery: BatteryStatusOut, timestamp: datetime | None) -> BatteryFacts:
    return BatteryFacts(
        timestamp=timestamp,
        soc_percent=_r(battery.soc_percent, 1) or 0.0,
        soh_percent=_r(battery.soh_percent, 1) or 0.0,
        status=battery.status,
        charge_kw=_r(battery.charge_power_kw, 3) or 0.0,
        discharge_kw=_r(battery.discharge_power_kw, 3) or 0.0,
        energy_available_kwh=_r(battery.energy_available_kwh, 3) or 0.0,
        temperature_c=_r(battery.temperature_c, 1),
        fault_code=battery.fault_code,
    )


def grid_facts(grid: GridStatusOut, timestamp: datetime | None) -> GridFacts:
    return GridFacts(
        timestamp=timestamp,
        status=grid.status,
        import_kw=_r(grid.import_power_kw, 3) or 0.0,
        export_kw=_r(grid.export_power_kw, 3) or 0.0,
        voltage_v=_r(grid.voltage_v, 1),
        frequency_hz=_r(grid.frequency_hz, 2),
        lifetime_import_kwh=_r(grid.total_import_kwh, 1),
        lifetime_export_kwh=_r(grid.total_export_kwh, 1),
    )


def devices_facts(devices: DevicesOut) -> DevicesFacts:
    rows = [
        DeviceFacts(
            device_id=d.device_id,
            name=d.device_name,
            type=d.device_type,
            state=d.current_state,
            current_power_kw=_r(d.current_power_kw, 3) or 0.0,
            rated_power_kw=_r(d.rated_power_kw, 3) or 0.0,
            priority=d.device_priority,
            critical=d.critical,
            controllable=d.controllable,
        )
        for d in devices.devices
    ]
    rows.sort(key=lambda d: d.current_power_kw, reverse=True)
    return DevicesFacts(
        timestamp=devices.timestamp,
        total_device_power_kw=_r(sum(d.current_power_kw for d in rows), 3) or 0.0,
        devices=rows,
    )


def weather_facts(weather: WeatherOut) -> WeatherFacts | None:
    w = weather.weather
    if w is None:
        return None
    return WeatherFacts(
        timestamp=w.timestamp,
        condition=_WMO_CONDITIONS.get(w.weather_code) if w.weather_code is not None else None,
        temperature_c=_r(w.temperature_c, 1) or 0.0,
        humidity_percent=_r(w.humidity_percent, 0),
        cloud_cover_percent=_r(w.cloud_cover_percent, 0) or 0.0,
        precipitation_mm=_r(w.precipitation_mm, 1),
        precipitation_probability_percent=_r(w.precipitation_probability_percent, 0) or 0.0,
        wind_speed_kmh=_r(w.wind_speed_kmh, 1),
        shortwave_radiation_wm2=_r(w.shortwave_radiation_wm2, 0) or 0.0,
        direct_radiation_wm2=_r(w.direct_radiation_wm2, 0),
        diffuse_radiation_wm2=_r(w.diffuse_radiation_wm2, 0),
        sunrise=w.sunrise,
        sunset=w.sunset,
        source=w.source,
        data_quality=w.data_quality,
    )


def daily_facts(daily: DailySummaryOut) -> DailyFacts:
    return DailyFacts(
        date=daily.date.isoformat(),
        timezone=daily.timezone,
        reading_count=daily.reading_count,
        period_end=daily.period_end,
        solar_generation_kwh=_r(daily.solar_generation_kwh, 3) or 0.0,
        home_consumption_kwh=_r(daily.home_consumption_kwh, 3) or 0.0,
        grid_import_kwh=_r(daily.grid_import_kwh, 3) or 0.0,
        grid_export_kwh=_r(daily.grid_export_kwh, 3) or 0.0,
        battery_charge_kwh=_r(daily.battery_charge_kwh, 3) or 0.0,
        battery_discharge_kwh=_r(daily.battery_discharge_kwh, 3) or 0.0,
        peak_load_kw=_r(daily.peak_load_kw, 3) or 0.0,
        estimated_savings_inr=_r(daily.estimated_savings),
        savings_method=daily.savings_method,
        tariff_rate=_r(daily.tariff_rate),
        export_credit_inr_per_kwh=_r(daily.export_credit_inr_per_kwh),
    )


def _peak(hours: list[HourFacts], attr: str) -> str | None:
    best = max(hours, key=lambda h: getattr(h, attr), default=None)
    if best is None or getattr(best, attr) <= 0:
        return None
    return best.hour


def hourly_facts(hourly: HourlySummaryOut) -> HourlyFacts:
    hours = [
        HourFacts(
            hour=bucket.hour_start.strftime("%H:00"),
            solar_kwh=_r(bucket.solar_generation_kwh, 3) or 0.0,
            home_kwh=_r(bucket.home_consumption_kwh, 3) or 0.0,
            import_kwh=_r(bucket.grid_import_kwh, 3) or 0.0,
            export_kwh=_r(bucket.grid_export_kwh, 3) or 0.0,
        )
        for bucket in hourly.buckets
        if bucket.reading_count > 0
    ]
    return HourlyFacts(
        date=hourly.date.isoformat(),
        timezone=hourly.timezone,
        hours=hours,
        peak_solar_hour=_peak(hours, "solar_kwh"),
        peak_consumption_hour=_peak(hours, "home_kwh"),
        peak_import_hour=_peak(hours, "import_kwh"),
    )


_TREND_STEADY_KW = 0.1


def trend_facts(records: Sequence[TelemetryRecord], *, window_minutes: int = 60) -> TrendFacts | None:
    """Summarise the ticks inside the window ending at the latest tick."""
    if not records:
        return None
    ordered = sorted(records, key=lambda r: r.timestamp)
    latest = ordered[-1]
    since = latest.timestamp - timedelta(minutes=window_minutes)
    window = [r for r in ordered if r.timestamp >= since]
    if len(window) < 2:
        return None
    first = window[0]
    solar = [r.solar_power_kw for r in window]
    load = [r.home_load_power_kw for r in window]
    delta = latest.solar_power_kw - first.solar_power_kw
    steady = max(_TREND_STEADY_KW, 0.05 * max(solar))
    direction = "rising" if delta > steady else "falling" if delta < -steady else "steady"
    # Each tick's interval energy covers the gap since the previous tick.
    covered = window[1:]
    return TrendFacts(
        window_minutes=round((latest.timestamp - first.timestamp).total_seconds() / 60),
        samples=len(window),
        solar_kw_start=_r(first.solar_power_kw, 3) or 0.0,
        solar_kw_now=_r(latest.solar_power_kw, 3) or 0.0,
        solar_kw_max=_r(max(solar), 3) or 0.0,
        solar_direction=direction,
        load_kw_avg=_r(sum(load) / len(load), 3) or 0.0,
        load_kw_max=_r(max(load), 3) or 0.0,
        battery_soc_start_percent=_r(first.battery_soc_percent, 1),
        battery_soc_now_percent=_r(latest.battery_soc_percent, 1),
        battery_soc_change_percent=_r(latest.battery_soc_percent - first.battery_soc_percent, 1),
        grid_import_kwh=_r(sum(r.grid_import_interval_kwh for r in covered), 3) or 0.0,
        grid_export_kwh=_r(sum(r.grid_export_interval_kwh for r in covered), 3) or 0.0,
    )


def _size(payload: Any) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str))


def _is_critical(row: dict[str, Any]) -> bool:
    return bool(row.get("critical")) or row.get("priority") == "critical"


def _drop_hourly_detail(p: dict[str, Any]) -> bool:
    hourly = p.get("hourly_summary")
    if not hourly or not hourly.get("hours"):
        return False
    hourly.pop("hours")
    return True


def _drop_idle_devices(p: dict[str, Any]) -> bool:
    devices = p.get("devices")
    if not devices or not devices.get("devices"):
        return False
    rows = devices["devices"]
    kept = [d for d in rows if d.get("current_power_kw", 0) > 0.01 or _is_critical(d)][:6]
    if len(kept) == len(rows):
        return False
    devices["devices"] = kept
    devices["idle_devices_omitted"] = len(rows) - len(kept)
    return True


_WEATHER_DETAIL = (
    "humidity_percent",
    "precipitation_mm",
    "wind_speed_kmh",
    "direct_radiation_wm2",
    "diffuse_radiation_wm2",
    "source",
)


def _drop_weather_detail(p: dict[str, Any]) -> bool:
    weather = p.get("weather")
    if not weather or not any(k in weather for k in _WEATHER_DETAIL):
        return False
    for key in _WEATHER_DETAIL:
        weather.pop(key, None)
    return True


def _compact_appliances(p: dict[str, Any]) -> bool:
    household = p.get("household")
    if not household or not household.get("appliances"):
        return False
    if all(set(a) <= {"name", "priority"} for a in household["appliances"]):
        return False
    household["appliances"] = [
        {"name": a["name"], "priority": "critical" if _is_critical(a) else a.get("priority")}
        for a in household["appliances"]
    ]
    return True


def _drop_live_warnings(p: dict[str, Any]) -> bool:
    live = p.get("live_energy")
    if not live or not live.get("warnings"):
        return False
    live.pop("warnings")
    return True


# Least useful detail first. Core numbers (power, SOC, kWh, tariff) are never dropped.
_REDUCERS: tuple[tuple[str, Any], ...] = (
    ("hourly_detail", _drop_hourly_detail),
    ("idle_devices", _drop_idle_devices),
    ("weather_detail", _drop_weather_detail),
    ("appliance_detail", _compact_appliances),
    ("live_warnings", _drop_live_warnings),
)


def fit_to_budget(payload: dict[str, Any], max_chars: int) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of the facts that fits `max_chars`, plus which reductions were applied."""
    fitted = json.loads(json.dumps(payload, default=str))
    applied: list[str] = []
    for name, reduce in _REDUCERS:
        if _size(fitted) <= max_chars:
            break
        if reduce(fitted):
            applied.append(name)
    return fitted, applied
