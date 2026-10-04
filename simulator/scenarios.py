"""
Scenario registry.

A scenario is a named real-world situation layered on top of the physics.
`auto` picks one per simulated day from season-aware weights so a single
continuous run (or backfill) covers many situations, and every tick carries
the active scenario name as a ground-truth label.

Add a scenario: add it to `SCENARIO_INFO`, then handle the name in the engine
it affects (weather overlay, solar, load/devices, battery, grid).
"""

from __future__ import annotations

from datetime import date

from simulator.engines.randomness import weighted_choice

SCENARIO_INFO: dict[str, str] = {
    "normal_day": "Typical day: real weather, normal habits, rare random events.",
    "cloudy_day": "Heavy cloud all day; diffuse light only, low PV.",
    "rainy_day": "Steady rain and thick cloud; very low PV, panels get washed.",
    "monsoon_storm": "Afternoon thunderstorm: gusts, intense rain, high outage risk.",
    "heatwave": "Air +5–6 °C: heavy AC/fan use, hot PV cells, warm battery.",
    "winter_cold": "Air −7 °C: long geyser runs, no AC, cool and efficient PV.",
    "battery_low": "Battery starts just above its reserve.",
    "battery_full": "Battery starts at 100 %; surplus goes to export or curtailment.",
    "battery_degraded": "Aged battery: SOH ~72 %, lower efficiency, runs warmer.",
    "battery_overheat": "Poorly ventilated battery: heats up, derates, may trip a fault.",
    "grid_outage": "Grid down all day; backup inverter limits apply.",
    "load_shedding": "Scheduled cuts 10:00–12:00 and 19:00–21:00.",
    "voltage_rise": "Feeder over-voltage at midday; inverter curtails export (volt-watt).",
    "high_evening_load": "Evening base load ×1.85 and AC forced on 17:00–23:00.",
    "guests_party": "Guests at home: more cooking, lighting, AC in the evening.",
    "vacation": "House empty: only the fridge and standby loads run.",
    "ev_heavy": "EV charged every night with a large energy need.",
    "dusty_panels": "Panels uncleaned for months: ~18 % soiling loss.",
    "partial_shading": "Tree/building shades the array in early morning and late afternoon.",
    "sensor_failure": "Irradiance sensor drop-out; data_quality=degraded.",
    "auto": "Season-aware mix: a different realistic scenario each day.",
}

SCENARIOS: tuple[str, ...] = tuple(SCENARIO_INFO)

# Only meaningful at start-up (initial SOC / ageing); not re-rolled daily.
STARTUP_ONLY = frozenset({"battery_low", "battery_full", "battery_degraded"})

# Weather-only scenarios. In synthetic weather modes `auto` lets the daily
# weather regime produce cloud/rain/storms naturally instead of overlaying.
WEATHER_OVERLAY_SCENARIOS = frozenset({"cloudy_day", "rainy_day", "monsoon_storm"})


def _auto_weights(day: date, latitude: float, synthetic_weather: bool) -> dict[str, float]:
    month = day.month
    northern = latitude >= 0
    hot_months = {4, 5, 6} if northern else {10, 11, 12}
    cold_months = {12, 1} if northern else {6, 7}
    monsoon_months = {6, 7, 8, 9} if northern else {12, 1, 2}
    weekend = day.weekday() >= 5

    weights: dict[str, float] = {
        "normal_day": 0.52,
        "high_evening_load": 0.05,
        "guests_party": 0.05 if weekend else 0.015,
        "vacation": 0.02,
        "ev_heavy": 0.03,
        "load_shedding": 0.03,
        "voltage_rise": 0.04 if month not in monsoon_months else 0.01,
        "dusty_panels": 0.03 if month not in monsoon_months else 0.0,
        "partial_shading": 0.02,
        "battery_overheat": 0.03 if month in hot_months else 0.005,
        "sensor_failure": 0.01,
        "grid_outage": 0.015,
        "heatwave": 0.10 if month in hot_months else 0.0,
        "winter_cold": 0.08 if (month in cold_months and abs(latitude) > 20) else 0.0,
    }
    if not synthetic_weather:
        weights["cloudy_day"] = 0.06
        weights["rainy_day"] = 0.12 if month in monsoon_months else 0.02
        weights["monsoon_storm"] = 0.05 if month in monsoon_months | {5, 10} else 0.0
    return weights


def scenario_for_day(
    base: str,
    day: date,
    *,
    latitude: float,
    seed: int,
    synthetic_weather: bool,
) -> str:
    if base != "auto":
        return base
    weights = _auto_weights(day, latitude, synthetic_weather)
    return weighted_choice(weights, seed, "auto-scenario", day)
