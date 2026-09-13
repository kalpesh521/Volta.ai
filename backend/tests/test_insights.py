"""Unit tests for assistant-derived fields (priority, peak, savings)."""
from app.modules.energy.insights import (
    HouseholdTariff,
    derive_device_priority,
    estimated_savings_inr,
    peak_load_kw,
)


def test_device_priority_ranking():
    assert derive_device_priority(critical=True, rated_power_kw=0.15) == "critical"
    assert derive_device_priority(critical=False, rated_power_kw=1.5) == "important"
    assert derive_device_priority(critical=False, rated_power_kw=0.6) == "flexible"
    assert derive_device_priority(critical=True, rated_power_kw=3.3) == "critical"


def test_peak_load_empty_and_max():
    assert peak_load_kw([]) == 0.0
    assert peak_load_kw([1.2, 2.4, 0.8]) == 2.4


def test_estimated_savings_self_consumed_plus_export():
    tariff = HouseholdTariff(
        tariff_type="Flat rate",
        tariff_rate=10.0,
        export_credit_inr_per_kwh=4.0,
        meter_type="Net metering",
        applicable=True,
    )
    # 3 kWh generated, 1 kWh exported → 2 kWh self-consumed
    assert estimated_savings_inr(
        solar_generation_kwh=3.0,
        grid_export_kwh=1.0,
        tariff=tariff,
    ) == 24.0


def test_estimated_savings_none_without_tariff():
    tariff = HouseholdTariff(None, None, None, None, False)
    assert (
        estimated_savings_inr(
            solar_generation_kwh=5.0,
            grid_export_kwh=1.0,
            tariff=tariff,
        )
        is None
    )
