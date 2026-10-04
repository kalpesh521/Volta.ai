"""
Deterministic energy analytics.

The LLM must not do arithmetic on energy values; it explains these results.
Every function is pure, unit-testable, and tolerant of missing inputs
(returns `None` or an `insufficient_data` verdict instead of guessing).
"""
from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum

from pydantic import BaseModel, Field

from app.modules.assistant.domain.context import (
    AIContext,
    BatteryFacts,
    DailyFacts,
    DeviceFacts,
    DevicesFacts,
    GridFacts,
    HouseholdFacts,
    LiveFacts,
    WeatherFacts,
)
from app.modules.assistant.domain.intents import ApplianceType
from app.modules.energy.insights import HouseholdTariff, estimated_savings_inr
from app.modules.energy.profile import APPLIANCE_DEVICE_SPECS

BALANCE_TOLERANCE_KW = 0.05
# Rough clear-sky fraction of nameplate a rooftop array delivers at midday.
_PEAK_YIELD_FACTOR = 0.75
_CLOUD_LOSS_FACTOR = 0.75
_SOLAR_RAMP_END = time(13, 0)
_DEFAULT_SUNRISE = time(6, 30)
_DEFAULT_SUNSET = time(18, 30)

_TYPICAL_RATING_KW: dict[str, tuple[str, float]] = {
    spec["device_type"]: (spec["device_name"], float(spec["rated_power_kw"]))
    for spec in APPLIANCE_DEVICE_SPECS.values()
}


class Analytic(StrEnum):
    SOLAR_SURPLUS = "calculate_solar_surplus"
    SELF_CONSUMPTION = "calculate_solar_self_consumption"
    ENERGY_INDEPENDENCE = "calculate_energy_independence"
    BACKUP_DURATION = "calculate_backup_duration"
    ESTIMATED_SAVINGS = "calculate_estimated_savings"
    ANOMALIES = "detect_energy_anomalies"
    APPLIANCE_FIT = "evaluate_appliance_run"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


class SolarSurplus(BaseModel):
    solar_kw: float
    load_kw: float
    surplus_kw: float
    state: str = Field(description="surplus | deficit | balanced")


class SelfConsumption(BaseModel):
    solar_generation_kwh: float
    grid_export_kwh: float
    self_consumed_kwh: float
    self_consumption_percent: float | None


class EnergyIndependence(BaseModel):
    home_consumption_kwh: float
    grid_import_kwh: float
    self_supplied_kwh: float
    independence_percent: float | None


class BackupDuration(BaseModel):
    battery_present: bool
    soc_percent: float | None = None
    reserve_soc_percent: float | None = None
    usable_energy_kwh: float | None = None
    load_kw: float | None = None
    backup_hours: float | None = None
    note: str


class SavingsEstimate(BaseModel):
    available: bool
    amount_inr: float | None = None
    self_consumed_kwh: float | None = None
    export_kwh: float | None = None
    tariff_rate_inr_per_kwh: float | None = None
    export_credit_inr_per_kwh: float | None = None
    method: str | None = None
    note: str | None = None


class Anomaly(BaseModel):
    code: str
    severity: Severity
    message: str


class ApplianceVerdict(StrEnum):
    RUN_NOW_ON_SOLAR = "run_now_on_solar"
    RUN_WITH_BATTERY_SUPPORT = "run_with_battery_support"
    WAIT_FOR_SOLAR = "wait_for_solar"
    GRID_IMPORT_REQUIRED = "grid_import_required"
    NOT_RECOMMENDED = "not_recommended"
    ALREADY_RUNNING = "already_running"
    INSUFFICIENT_DATA = "insufficient_data"


class ApplianceAdvice(BaseModel):
    appliance: str | None
    device_name: str | None = None
    rated_power_kw: float | None = None
    rating_source: str | None = None
    currently_on: bool = False
    solar_surplus_kw: float | None = None
    solar_covers_percent: float | None = None
    battery_headroom_kwh: float | None = None
    expected_grid_import_kw: float | None = None
    estimated_grid_cost_inr_per_hour: float | None = None
    verdict: ApplianceVerdict
    reasons: list[str] = Field(default_factory=list)


def _pct(part: float, whole: float) -> float | None:
    if whole <= 1e-9:
        return None
    return round(min(100.0, max(0.0, part / whole * 100.0)), 1)


def calculate_solar_surplus(live: LiveFacts) -> SolarSurplus:
    surplus = round(live.solar_kw - live.load_kw, 3)
    if surplus > BALANCE_TOLERANCE_KW:
        state = "surplus"
    elif surplus < -BALANCE_TOLERANCE_KW:
        state = "deficit"
    else:
        state = "balanced"
    return SolarSurplus(solar_kw=live.solar_kw, load_kw=live.load_kw, surplus_kw=surplus, state=state)


def calculate_solar_self_consumption(daily: DailyFacts) -> SelfConsumption:
    export = max(0.0, daily.grid_export_kwh)
    self_consumed = round(max(0.0, daily.solar_generation_kwh - export), 3)
    return SelfConsumption(
        solar_generation_kwh=daily.solar_generation_kwh,
        grid_export_kwh=export,
        self_consumed_kwh=self_consumed,
        self_consumption_percent=_pct(self_consumed, daily.solar_generation_kwh),
    )


def calculate_energy_independence(daily: DailyFacts) -> EnergyIndependence:
    imported = max(0.0, daily.grid_import_kwh)
    self_supplied = round(max(0.0, daily.home_consumption_kwh - imported), 3)
    return EnergyIndependence(
        home_consumption_kwh=daily.home_consumption_kwh,
        grid_import_kwh=imported,
        self_supplied_kwh=self_supplied,
        independence_percent=_pct(self_supplied, daily.home_consumption_kwh),
    )


def battery_headroom_kwh(battery: BatteryFacts, household: HouseholdFacts | None) -> float:
    """Energy above the reserve SOC the owner wants to keep."""
    reserve = household.battery_minimum_soc_percent if household else 20.0
    capacity = household.battery_capacity_kwh if household else 0.0
    if capacity > 0:
        return round(max(0.0, capacity * (battery.soc_percent - reserve) / 100.0), 3)
    if battery.soc_percent <= 0:
        return 0.0
    above = max(0.0, (battery.soc_percent - reserve) / battery.soc_percent)
    return round(battery.energy_available_kwh * above, 3)


def calculate_backup_duration(
    battery: BatteryFacts | None,
    household: HouseholdFacts | None,
    load_kw: float | None,
) -> BackupDuration:
    present = bool(household.battery_present) if household else battery is not None
    if not present or battery is None:
        return BackupDuration(battery_present=False, note="This home has no battery backup.")
    reserve = household.battery_minimum_soc_percent if household else 20.0
    usable = battery_headroom_kwh(battery, household)
    if load_kw is None or load_kw <= 0.05:
        return BackupDuration(
            battery_present=True,
            soc_percent=battery.soc_percent,
            reserve_soc_percent=reserve,
            usable_energy_kwh=usable,
            load_kw=load_kw,
            note="Home load is near zero, so backup duration is not meaningful right now.",
        )
    hours = round(usable / load_kw, 1)
    return BackupDuration(
        battery_present=True,
        soc_percent=battery.soc_percent,
        reserve_soc_percent=reserve,
        usable_energy_kwh=usable,
        load_kw=load_kw,
        backup_hours=hours,
        note="Assumes the current load stays constant and the reserve SOC is protected.",
    )


def calculate_estimated_savings(
    daily: DailyFacts, household: HouseholdFacts | None
) -> SavingsEstimate:
    rate = daily.tariff_rate if daily.tariff_rate is not None else (household.tariff_rate if household else None)
    credit = (
        daily.export_credit_inr_per_kwh
        if daily.export_credit_inr_per_kwh is not None
        else (household.export_credit_inr_per_kwh if household else None)
    )
    tariff = HouseholdTariff(
        tariff_type=household.tariff_type if household else None,
        tariff_rate=rate,
        export_credit_inr_per_kwh=credit,
        meter_type=household.meter_type if household else None,
        applicable=rate is not None and credit is not None,
    )
    amount = estimated_savings_inr(
        solar_generation_kwh=daily.solar_generation_kwh,
        grid_export_kwh=daily.grid_export_kwh,
        tariff=tariff,
    )
    if amount is None:
        return SavingsEstimate(
            available=False,
            note="No numeric tariff is on file for this home, so savings cannot be estimated.",
        )
    export = max(0.0, daily.grid_export_kwh)
    return SavingsEstimate(
        available=True,
        amount_inr=amount,
        self_consumed_kwh=round(max(0.0, daily.solar_generation_kwh - export), 3),
        export_kwh=round(export, 3),
        tariff_rate_inr_per_kwh=rate,
        export_credit_inr_per_kwh=credit,
        method=tariff.savings_method,
        note="Value of solar versus buying the same energy from the grid; not a utility bill.",
    )


def detect_energy_anomalies(
    *,
    live: LiveFacts | None,
    battery: BatteryFacts | None,
    grid: GridFacts | None,
    household: HouseholdFacts | None,
    weather: WeatherFacts | None,
) -> list[Anomaly]:
    found: list[Anomaly] = []

    def add(code: str, severity: Severity, message: str) -> None:
        found.append(Anomaly(code=code, severity=severity, message=message))

    if live is not None:
        quality = live.data_quality.lower()
        if quality == "degraded":
            add("data_quality_degraded", Severity.WARNING, "Telemetry quality is degraded.")
        elif quality == "fallback":
            add("data_quality_fallback", Severity.INFO, "Weather inputs used fallback values.")
        if live.energy_balance_status != "valid":
            add(
                "energy_balance_mismatch",
                Severity.WARNING,
                "Measured power flows do not balance; readings may be inaccurate.",
            )
        unserved = live.flows_kw.get("unserved_load_kw", 0.0)
        if unserved > 0.02:
            add(
                "unserved_load",
                Severity.CRITICAL,
                f"{unserved:.2f} kW of home demand is not being supplied.",
            )
        if household is not None:
            if household.inverter_capacity_kw > 0 and live.load_kw > 0.9 * household.inverter_capacity_kw:
                add(
                    "load_near_inverter_limit",
                    Severity.WARNING,
                    f"Home load {live.load_kw:.2f} kW is close to the "
                    f"{household.inverter_capacity_kw:.1f} kW inverter limit.",
                )
            if (
                weather is not None
                and weather.shortwave_radiation_wm2 >= 600
                and household.solar_capacity_kwp > 0
                and live.solar_kw < 0.15 * household.solar_capacity_kwp
            ):
                add(
                    "solar_underperforming",
                    Severity.WARNING,
                    "Strong sunlight but low solar output; panels may be shaded, dirty, or faulty.",
                )

    if battery is not None:
        if battery.fault_code:
            add("battery_fault", Severity.CRITICAL, f"Battery reports fault code {battery.fault_code}.")
        if battery.temperature_c is not None and battery.temperature_c > 45:
            add(
                "battery_hot",
                Severity.WARNING,
                f"Battery temperature is high at {battery.temperature_c:.1f} °C.",
            )
        if household is not None and household.battery_present:
            reserve = household.battery_minimum_soc_percent
            if battery.soc_percent < reserve - 1:
                add(
                    "battery_below_reserve",
                    Severity.WARNING,
                    f"Battery at {battery.soc_percent:.0f}% is below the {reserve:.0f}% reserve.",
                )

    if grid is not None and household is not None:
        if household.grid_available and grid.status.lower() == "outage":
            add("grid_outage", Severity.WARNING, "Grid is down; the home is running on solar and battery.")
        if household.zero_export_mode and grid.export_kw > 0.05:
            add(
                "export_in_zero_export_mode",
                Severity.WARNING,
                "Power is being exported although the meter is configured for no export.",
            )
        if battery is not None and household.battery_present and battery.status != "unavailable":
            reserve = household.battery_minimum_soc_percent
            if grid.import_kw > 0.1 and battery.soc_percent > reserve + 10 and battery.discharge_kw < 0.05:
                add(
                    "importing_with_battery_available",
                    Severity.INFO,
                    "Importing from the grid while the battery holds charge above reserve.",
                )
            if grid.export_kw > 0.1 and battery.soc_percent < 95 and battery.charge_kw < 0.05:
                add(
                    "exporting_while_battery_not_full",
                    Severity.INFO,
                    "Exporting solar while the battery is not full and not charging.",
                )

    found.sort(key=lambda a: _SEVERITY_RANK[a.severity])
    return found


def _local_clock(stamp: datetime) -> time:
    return stamp.timetz().replace(tzinfo=None)


def _daylight_bounds(weather: WeatherFacts | None, stamp: datetime) -> tuple[time, time]:
    if weather is not None and weather.sunrise and weather.sunset:
        tz = stamp.tzinfo
        sunrise = weather.sunrise.astimezone(tz) if tz else weather.sunrise
        sunset = weather.sunset.astimezone(tz) if tz else weather.sunset
        return _local_clock(sunrise), _local_clock(sunset)
    return _DEFAULT_SUNRISE, _DEFAULT_SUNSET


def expected_peak_solar_kw(household: HouseholdFacts, weather: WeatherFacts | None) -> float:
    cloud = (weather.cloud_cover_percent / 100.0) if weather else 0.3
    return round(
        household.solar_capacity_kwp * _PEAK_YIELD_FACTOR * (1.0 - _CLOUD_LOSS_FACTOR * cloud),
        3,
    )


def _resolve_device(
    appliance: ApplianceType,
    devices: DevicesFacts | None,
    household: HouseholdFacts | None,
) -> tuple[str, float, str, DeviceFacts | None, bool]:
    """(name, rated_kw, rating_source, live_reading, is_tracked)."""
    if devices is not None:
        for device in devices.devices:
            if device.type == appliance.value:
                return device.name, device.rated_power_kw, "live_reading", device, True
    if household is not None:
        for configured in household.appliances:
            if configured.type == appliance.value:
                return configured.name, configured.rated_power_kw, "household_profile", None, True
    name, rated = _TYPICAL_RATING_KW.get(
        appliance.value, (appliance.value.replace("_", " ").title(), 1.0)
    )
    return name, rated, "typical_rating", None, False


def evaluate_appliance_run(
    *,
    appliance: ApplianceType | None,
    live: LiveFacts | None,
    battery: BatteryFacts | None,
    devices: DevicesFacts | None,
    household: HouseholdFacts | None,
    weather: WeatherFacts | None,
) -> ApplianceAdvice:
    if appliance is None:
        return ApplianceAdvice(
            appliance=None,
            verdict=ApplianceVerdict.INSUFFICIENT_DATA,
            reasons=["The question did not name a specific appliance."],
        )
    name, rated, source, reading, tracked = _resolve_device(appliance, devices, household)
    reasons: list[str] = []
    if not tracked:
        reasons.append(f"{name} is not a tracked appliance; using a typical {rated:.2f} kW rating.")

    if live is None:
        return ApplianceAdvice(
            appliance=appliance.value,
            device_name=name,
            rated_power_kw=rated,
            rating_source=source,
            verdict=ApplianceVerdict.INSUFFICIENT_DATA,
            reasons=[*reasons, "No live energy data is available."],
        )

    surplus = round(live.solar_kw - live.load_kw, 3)
    currently_on = reading.is_on if reading else False
    headroom = battery_headroom_kwh(battery, household) if battery is not None else None
    tariff = household.tariff_rate if household else None

    # Running device is already inside load_kw; judge its own draw against solar.
    extra_kw = reading.current_power_kw if currently_on and reading else rated
    available_solar = max(0.0, surplus + (extra_kw if currently_on else 0.0))
    solar_cover_kw = min(extra_kw, available_solar)
    deficit_kw = round(max(0.0, extra_kw - solar_cover_kw), 3)
    covers_pct = _pct(solar_cover_kw, extra_kw)
    grid_cost = round(deficit_kw * tariff, 2) if tariff is not None and deficit_kw > 0 else None

    advice = ApplianceAdvice(
        appliance=appliance.value,
        device_name=name,
        rated_power_kw=rated,
        rating_source=source,
        currently_on=currently_on,
        solar_surplus_kw=surplus,
        solar_covers_percent=covers_pct,
        battery_headroom_kwh=headroom,
        expected_grid_import_kw=None,
        estimated_grid_cost_inr_per_hour=None,
        verdict=ApplianceVerdict.INSUFFICIENT_DATA,
        reasons=reasons,
    )

    if currently_on:
        advice.verdict = ApplianceVerdict.ALREADY_RUNNING
        advice.reasons.append(f"{name} is already running at {extra_kw:.2f} kW.")
        return advice

    if deficit_kw <= 0.05 * extra_kw:
        advice.verdict = ApplianceVerdict.RUN_NOW_ON_SOLAR
        advice.expected_grid_import_kw = 0.0
        advice.reasons.append(
            f"Solar surplus of {surplus:.2f} kW covers the {extra_kw:.2f} kW appliance."
        )
        return advice

    preserve_backup = household is not None and household.primary_goal == "preserve_backup"
    battery_usable = (
        battery is not None
        and battery.status != "unavailable"
        and headroom is not None
        and headroom >= deficit_kw
        and not preserve_backup
    )
    if battery_usable:
        advice.verdict = ApplianceVerdict.RUN_WITH_BATTERY_SUPPORT
        advice.expected_grid_import_kw = 0.0
        advice.reasons.append(
            f"Solar covers {covers_pct or 0:.0f}%; the battery has {headroom:.2f} kWh above reserve "
            f"to cover the remaining {deficit_kw:.2f} kW for about an hour."
        )
        return advice
    if preserve_backup and headroom:
        advice.reasons.append("Battery is kept for backup because the home goal is preserve_backup.")

    clock = _local_clock(live.timestamp)
    sunrise, sunset = _daylight_bounds(weather, live.timestamp)
    solar_still_rising = sunrise <= clock < min(_SOLAR_RAMP_END, sunset)
    before_dawn = clock < sunrise
    peak_kw = expected_peak_solar_kw(household, weather) if household else 0.0
    flexible = reading is None or not reading.critical

    if flexible and (solar_still_rising or before_dawn) and peak_kw - max(live.load_kw, 0.0) >= extra_kw * 0.8:
        advice.verdict = ApplianceVerdict.WAIT_FOR_SOLAR
        advice.expected_grid_import_kw = deficit_kw
        advice.estimated_grid_cost_inr_per_hour = grid_cost
        advice.reasons.append(
            f"Solar is expected to reach about {peak_kw:.1f} kW around midday, "
            f"enough for the {extra_kw:.2f} kW appliance."
        )
        return advice

    grid_ok = household.grid_available if household else True
    if grid_ok:
        advice.verdict = ApplianceVerdict.GRID_IMPORT_REQUIRED
        advice.expected_grid_import_kw = deficit_kw
        advice.estimated_grid_cost_inr_per_hour = grid_cost
        advice.reasons.append(
            f"Running it now would import about {deficit_kw:.2f} kW from the grid."
        )
        if clock >= _SOLAR_RAMP_END:
            advice.reasons.append("For the lowest cost, run it between 10:00 and 14:00 on a sunny day.")
        return advice

    advice.verdict = ApplianceVerdict.NOT_RECOMMENDED
    advice.expected_grid_import_kw = None
    advice.reasons.append("Off-grid home without enough solar surplus or battery headroom right now.")
    return advice


class AnalyticsBundle(BaseModel):
    solar_surplus: SolarSurplus | None = None
    self_consumption: SelfConsumption | None = None
    energy_independence: EnergyIndependence | None = None
    backup: BackupDuration | None = None
    savings: SavingsEstimate | None = None
    anomalies: list[Anomaly] | None = None
    appliance: ApplianceAdvice | None = None
    computed: list[Analytic] = Field(default_factory=list)

    def prompt_payload(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, exclude={"computed"})


def run_analytics(
    selected: list[Analytic] | tuple[Analytic, ...],
    context: AIContext,
    *,
    appliance: ApplianceType | None = None,
) -> AnalyticsBundle:
    """Run each selected analytic whose inputs are present. Missing inputs → skipped."""
    bundle = AnalyticsBundle()
    live, daily, household = context.live_energy, context.today_summary, context.household
    devices = context.devices
    for analytic in selected:
        if analytic is Analytic.SOLAR_SURPLUS and live is not None:
            bundle.solar_surplus = calculate_solar_surplus(live)
        elif analytic is Analytic.SELF_CONSUMPTION and daily is not None:
            bundle.self_consumption = calculate_solar_self_consumption(daily)
        elif analytic is Analytic.ENERGY_INDEPENDENCE and daily is not None:
            bundle.energy_independence = calculate_energy_independence(daily)
        elif analytic is Analytic.BACKUP_DURATION and (context.battery or household):
            bundle.backup = calculate_backup_duration(
                context.battery, household, live.load_kw if live else None
            )
        elif analytic is Analytic.ESTIMATED_SAVINGS and daily is not None:
            bundle.savings = calculate_estimated_savings(daily, household)
        elif analytic is Analytic.ANOMALIES and (live or context.battery or context.grid):
            bundle.anomalies = detect_energy_anomalies(
                live=live,
                battery=context.battery,
                grid=context.grid,
                household=household,
                weather=context.weather,
            )
        elif analytic is Analytic.APPLIANCE_FIT:
            bundle.appliance = evaluate_appliance_run(
                appliance=appliance,
                live=live,
                battery=context.battery,
                devices=devices,
                household=household,
                weather=context.weather,
            )
        else:
            continue
        bundle.computed.append(analytic)
    return bundle
