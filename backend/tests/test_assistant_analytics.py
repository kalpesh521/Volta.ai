"""Deterministic analytics: the numbers the LLM is allowed to quote."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.modules.assistant.domain.analytics import (
    Analytic,
    ApplianceVerdict,
    calculate_backup_duration,
    calculate_energy_independence,
    calculate_estimated_savings,
    calculate_solar_self_consumption,
    calculate_solar_surplus,
    detect_energy_anomalies,
    evaluate_appliance_run,
    run_analytics,
)
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
from app.modules.assistant.domain.freshness import FreshnessStatus, assess_freshness
from app.modules.assistant.domain.intents import ApplianceType

TZ = ZoneInfo("Asia/Kolkata")
NOON = datetime(2026, 10, 2, 12, 0, tzinfo=TZ)


def _household(**overrides) -> HouseholdFacts:
    base = dict(
        system_type="Hybrid",
        solar_capacity_kwp=4.0,
        inverter_capacity_kw=5.0,
        battery_present=True,
        battery_capacity_kwh=10.0,
        battery_usable_capacity_kwh=8.0,
        battery_minimum_soc_percent=20.0,
        grid_available=True,
        zero_export_mode=False,
        tariff_type="Flat rate",
        tariff_rate=8.5,
        export_credit_inr_per_kwh=6.2,
        primary_goal="maximize_self_consumption",
    )
    base.update(overrides)
    return HouseholdFacts(**base)


def _live(
    ts: datetime = NOON,
    *,
    solar: float = 3.0,
    load: float = 1.0,
    flows: dict[str, float] | None = None,
) -> LiveFacts:
    return LiveFacts(
        timestamp=ts,
        data_source="simulator",
        data_quality="simulated",
        solar_kw=solar,
        load_kw=load,
        flows_kw=flows or {},
        energy_balance_status="valid",
        solar_today_kwh=5.0,
        consumption_today_kwh=4.0,
    )


def _battery(soc: float = 60.0, **kw) -> BatteryFacts:
    base = dict(
        timestamp=NOON,
        soc_percent=soc,
        soh_percent=98.0,
        status="idle",
        charge_kw=0.0,
        discharge_kw=0.0,
        energy_available_kwh=6.0,
    )
    base.update(kw)
    return BatteryFacts(**base)


def _daily(**kw) -> DailyFacts:
    base = dict(
        date="2026-10-02",
        timezone="Asia/Kolkata",
        reading_count=100,
        solar_generation_kwh=10.0,
        home_consumption_kwh=8.0,
        grid_import_kwh=2.0,
        grid_export_kwh=4.0,
        battery_charge_kwh=1.0,
        battery_discharge_kwh=1.0,
        peak_load_kw=3.0,
        tariff_rate=8.5,
        export_credit_inr_per_kwh=6.2,
    )
    base.update(kw)
    return DailyFacts(**base)


def _weather(**kw) -> WeatherFacts:
    base = dict(
        timestamp=NOON,
        temperature_c=30.0,
        cloud_cover_percent=10.0,
        precipitation_probability_percent=0.0,
        shortwave_radiation_wm2=750.0,
        data_quality="simulated",
    )
    base.update(kw)
    return WeatherFacts(**base)


def test_solar_surplus_states():
    assert calculate_solar_surplus(_live(solar=3.0, load=1.0)).state == "surplus"
    assert calculate_solar_surplus(_live(solar=3.0, load=1.0)).surplus_kw == 2.0
    assert calculate_solar_surplus(_live(solar=0.5, load=2.0)).state == "deficit"
    assert calculate_solar_surplus(_live(solar=1.0, load=1.02)).state == "balanced"


def test_self_consumption_and_independence():
    sc = calculate_solar_self_consumption(_daily())
    assert sc.self_consumed_kwh == 6.0
    assert sc.self_consumption_percent == 60.0
    ind = calculate_energy_independence(_daily())
    assert ind.self_supplied_kwh == 6.0
    assert ind.independence_percent == 75.0
    assert calculate_solar_self_consumption(_daily(solar_generation_kwh=0, grid_export_kwh=0)).self_consumption_percent is None


def test_backup_duration_protects_reserve():
    result = calculate_backup_duration(_battery(soc=60.0), _household(), load_kw=2.0)
    assert result.usable_energy_kwh == 4.0  # 10 kWh × (60 − 20)%
    assert result.backup_hours == 2.0
    no_battery = calculate_backup_duration(None, _household(battery_present=False), load_kw=2.0)
    assert no_battery.battery_present is False
    idle_load = calculate_backup_duration(_battery(), _household(), load_kw=0.0)
    assert idle_load.backup_hours is None


def test_estimated_savings_reuses_energy_formula():
    savings = calculate_estimated_savings(_daily(), _household())
    assert savings.available is True
    assert savings.amount_inr == round(6.0 * 8.5 + 4.0 * 6.2, 2)
    missing = calculate_estimated_savings(
        _daily(tariff_rate=None, export_credit_inr_per_kwh=None),
        _household(tariff_rate=None, export_credit_inr_per_kwh=None),
    )
    assert missing.available is False


def test_anomalies_detect_outage_unserved_and_reserve():
    anomalies = detect_energy_anomalies(
        live=_live(solar=0.0, load=2.0, flows={"unserved_load_kw": 0.5}),
        battery=_battery(soc=12.0),
        grid=GridFacts(status="outage", import_kw=0.0, export_kw=0.0),
        household=_household(),
        weather=None,
    )
    codes = [a.code for a in anomalies]
    assert codes[0] == "unserved_load"
    assert {"grid_outage", "battery_below_reserve"} <= set(codes)


def test_anomaly_import_while_battery_available():
    anomalies = detect_energy_anomalies(
        live=_live(solar=0.0, load=1.5),
        battery=_battery(soc=80.0),
        grid=GridFacts(status="available", import_kw=1.5, export_kw=0.0),
        household=_household(),
        weather=None,
    )
    assert "importing_with_battery_available" in {a.code for a in anomalies}


def test_appliance_runs_on_solar_surplus():
    advice = evaluate_appliance_run(
        appliance=ApplianceType.WASHING_MACHINE,
        live=_live(solar=3.0, load=1.0),
        battery=_battery(),
        devices=None,
        household=_household(),
        weather=_weather(),
    )
    assert advice.verdict is ApplianceVerdict.RUN_NOW_ON_SOLAR
    assert advice.rated_power_kw == 0.6
    assert advice.rating_source == "typical_rating"


def test_appliance_uses_battery_headroom():
    advice = evaluate_appliance_run(
        appliance=ApplianceType.WATER_HEATER,
        live=_live(solar=1.0, load=0.5),
        battery=_battery(soc=80.0),
        devices=None,
        household=_household(),
        weather=_weather(),
    )
    assert advice.verdict is ApplianceVerdict.RUN_WITH_BATTERY_SUPPORT
    assert advice.expected_grid_import_kw == 0.0


def test_appliance_waits_for_morning_solar():
    morning = NOON.replace(hour=8)
    advice = evaluate_appliance_run(
        appliance=ApplianceType.WATER_HEATER,
        live=_live(morning, solar=0.4, load=0.5),
        battery=_battery(soc=21.0),
        devices=None,
        household=_household(),
        weather=_weather(timestamp=morning),
    )
    assert advice.verdict is ApplianceVerdict.WAIT_FOR_SOLAR


def test_appliance_evening_needs_grid_with_cost():
    evening = NOON.replace(hour=20)
    advice = evaluate_appliance_run(
        appliance=ApplianceType.WATER_HEATER,
        live=_live(evening, solar=0.0, load=0.5),
        battery=_battery(soc=21.0),
        devices=None,
        household=_household(),
        weather=None,
    )
    assert advice.verdict is ApplianceVerdict.GRID_IMPORT_REQUIRED
    assert advice.expected_grid_import_kw == 2.0
    assert advice.estimated_grid_cost_inr_per_hour == 17.0


def test_appliance_preserve_backup_goal_skips_battery():
    evening = NOON.replace(hour=20)
    advice = evaluate_appliance_run(
        appliance=ApplianceType.WATER_HEATER,
        live=_live(evening, solar=0.0, load=0.5),
        battery=_battery(soc=90.0),
        devices=None,
        household=_household(primary_goal="preserve_backup"),
        weather=None,
    )
    assert advice.verdict is ApplianceVerdict.GRID_IMPORT_REQUIRED


def test_appliance_already_running_and_unknown():
    running = DevicesFacts(
        timestamp=NOON,
        total_device_power_kw=1.4,
        devices=[
            DeviceFacts(
                device_id="dev_air_conditioner",
                name="Air conditioner",
                type="air_conditioner",
                state="on",
                current_power_kw=1.4,
                rated_power_kw=1.5,
                priority="important",
                critical=False,
                controllable=True,
            )
        ],
    )
    advice = evaluate_appliance_run(
        appliance=ApplianceType.AIR_CONDITIONER,
        live=_live(solar=3.0, load=2.0),
        battery=_battery(),
        devices=running,
        household=_household(),
        weather=None,
    )
    assert advice.verdict is ApplianceVerdict.ALREADY_RUNNING
    assert advice.solar_covers_percent == 100.0
    unknown = evaluate_appliance_run(
        appliance=None, live=None, battery=None, devices=None, household=None, weather=None
    )
    assert unknown.verdict is ApplianceVerdict.INSUFFICIENT_DATA


def test_appliance_off_grid_not_recommended():
    evening = NOON.replace(hour=21)
    advice = evaluate_appliance_run(
        appliance=ApplianceType.EV_CHARGER,
        live=_live(evening, solar=0.0, load=0.5),
        battery=_battery(soc=25.0),
        devices=None,
        household=_household(system_type="Off-grid", grid_available=False),
        weather=None,
    )
    assert advice.verdict is ApplianceVerdict.NOT_RECOMMENDED


def test_run_analytics_skips_missing_inputs():
    bundle = run_analytics(
        [Analytic.SOLAR_SURPLUS, Analytic.ESTIMATED_SAVINGS, Analytic.ANOMALIES],
        AIContext(today_summary=_daily(), household=_household()),
    )
    assert bundle.computed == [Analytic.ESTIMATED_SAVINGS]
    assert bundle.solar_surplus is None


@pytest.mark.parametrize(
    ("age", "status"),
    [(timedelta(seconds=30), FreshnessStatus.FRESH), (timedelta(hours=2), FreshnessStatus.STALE)],
)
def test_freshness(age, status):
    result = assess_freshness(NOON - age, now=NOON, threshold_seconds=600)
    assert result.status is status
    if status is FreshnessStatus.STALE:
        assert "2 hours old" in (result.message or "")


def test_freshness_missing_and_future_skew():
    assert assess_freshness(None, now=NOON, threshold_seconds=600).status is FreshnessStatus.MISSING
    ahead = assess_freshness(NOON + timedelta(seconds=20), now=NOON, threshold_seconds=600)
    assert ahead.status is FreshnessStatus.FRESH
    assert ahead.age_seconds == 0
