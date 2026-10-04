"""
Simulator configuration.

Values load from environment / `.env` when present, otherwise the defaults
below are used. Extra env vars are ignored so new keys can be added later
without breaking older runs.

To add a new setting: declare it on `SimulatorConfig` and (optionally) on
`.env.example`. Generators read only the fields they need.

To add/remove a device: edit `DEFAULT_DEVICE_CATALOG` (or set
`DEVICE_CATALOG_JSON` in `.env`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from simulator.engines.randomness import stable_hash
from simulator.scenarios import SCENARIOS

SIMULATOR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SIMULATOR_DIR.parent

load_dotenv(SIMULATOR_DIR / ".env")
load_dotenv()

WeatherMode = Literal["live", "fallback", "historical-style"]

WEATHER_MODES: tuple[str, ...] = ("live", "fallback", "historical-style")

# Open-Meteo hourly keys. Add or remove entries here AND update
# `WEATHER_FIELD_MAP` in models.py if the telemetry weather object should change.
# Radiation uses the *_instant variants: the plain variables are averages of
# the preceding hour, which shifts an interpolated PV curve ~30 min late.
OPEN_METEO_HOURLY_FIELDS: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "cloud_cover",
    "wind_speed_10m",
    "wind_gusts_10m",
    "shortwave_radiation_instant",
    "direct_radiation_instant",
    "diffuse_radiation_instant",
    "direct_normal_irradiance_instant",
    "weather_code",
)

OPEN_METEO_DAILY_FIELDS: tuple[str, ...] = ("sunrise", "sunset")

DEFAULT_DEVICE_CATALOG: list[dict[str, Any]] = [
    {
        "device_id": "dev_refrigerator",
        "device_name": "Refrigerator",
        "device_type": "refrigerator",
        "rated_power_kw": 0.15,
        "critical": True,
        "controllable": False,
        "schedule": {"mode": "cyclic", "on_minutes": 25, "cycle_minutes": 45},
    },
    {
        "device_id": "dev_washing_machine",
        "device_name": "Washing machine",
        "device_type": "washing_machine",
        "rated_power_kw": 0.60,
        "critical": False,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [{"start": "07:00", "end": "08:00"}],
        },
    },
    {
        "device_id": "dev_water_heater",
        "device_name": "Water heater",
        "device_type": "water_heater",
        "rated_power_kw": 2.00,
        "critical": False,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [
                {"start": "06:00", "end": "07:00"},
                {"start": "18:00", "end": "19:00"},
            ],
        },
    },
    {
        "device_id": "dev_air_conditioner",
        "device_name": "Air conditioner",
        "device_type": "air_conditioner",
        "rated_power_kw": 1.50,
        "critical": False,
        "controllable": True,
        "schedule": {
            "mode": "temperature_or_windows",
            "on_above_c": 29.0,
            "windows": [
                {"start": "13:00", "end": "16:00"},
                {"start": "21:00", "end": "23:00"},
            ],
        },
    },
    {
        "device_id": "dev_water_pump",
        "device_name": "Water pump",
        "device_type": "water_pump",
        "rated_power_kw": 0.75,
        "critical": False,
        "controllable": True,
        "schedule": {
            "mode": "windows",
            "windows": [{"start": "06:30", "end": "07:00"}],
        },
    },
]


class SimulatorConfig(BaseSettings):
    """All tunables for the standalone generator. Extra env keys are ignored."""

    model_config = SettingsConfigDict(
        env_file=str(SIMULATOR_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    household_id: str = "home_001"
    timezone: str = "Asia/Kolkata"
    latitude: float = 18.6298
    longitude: float = 73.7997
    location_name: str = "Pimpri-Chinchwad"
    location_country: str = "India"
    location_country_code: str = "IN"
    location_admin1: str = "Maharashtra"
    location_elevation_m: float = 570.0
    location_id: int = 0
    location_source: str = "default"
    location_label: str = "Pimpri-Chinchwad, Maharashtra, India"
    geocode_on_start: bool = True
    geocoding_base_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    geocoding_language: str = "en"
    geocoding_timeout_seconds: float = 15.0

    system_type: str = "Hybrid"
    solar_capacity_kwp: float = 5.0
    inverter_capacity_kw: float = 5.0
    # DC-side derate only (wiring, mismatch, LID, nameplate tolerance).
    # Temperature, soiling, reflection and inverter losses are modelled separately.
    solar_efficiency: float = 0.92
    shading_factor: float = 0.97
    pv_temp_coefficient_per_c: float = 0.0037
    # None = auto: tilt ≈ |latitude| (5–35°), facing the equator.
    panel_tilt_deg: float | None = None
    panel_azimuth_deg: float | None = None
    ground_albedo: float = 0.2
    panel_age_years: float = 1.0
    panel_degradation_per_year: float = 0.005
    clear_sky_turbidity: float = 0.92
    cloud_transients: bool = True
    soiling_rate_per_day: float = 0.0025
    soiling_max_loss: float = 0.30
    initial_soiling_loss_percent: float = 2.0
    panel_cleaning_interval_days: int = 15

    household_occupants: int = 4
    work_from_home: bool = False

    battery_present: bool = True
    battery_capacity_kwh: float = 10.0
    battery_usable_capacity_kwh: float = 8.0
    initial_battery_soc_percent: float = 60.0
    battery_minimum_soc_percent: float = 20.0
    battery_max_charge_power_kw: float = 5.0
    battery_max_discharge_power_kw: float = 5.0
    battery_charge_efficiency: float = 0.95
    battery_discharge_efficiency: float = 0.95
    battery_can_charge_from_grid: bool = False
    battery_soh_percent: float = 98.0
    battery_cycle_life: int = 6000
    battery_taper_start_soc: float = 90.0
    battery_thermal_time_constant_minutes: float = 90.0
    battery_max_temperature_c: float = 55.0
    # Kept in the battery for outages when primary_goal=preserve_backup.
    battery_backup_reserve_percent: float = 50.0
    grid_charge_target_soc_percent: float = 80.0

    # True = the home is grid-connected (On-grid / Hybrid). Momentary outages
    # come from force_grid_outage, grid_outage_window, load_shedding_windows
    # or the random outage model.
    grid_available: bool = True
    force_grid_outage: bool = False
    zero_export_mode: bool = False
    zero_export_limit_kw: float = 0.0
    grid_nominal_voltage_v: float = 230.0
    grid_nominal_frequency_hz: float = 50.0
    grid_outage_window: str = ""
    load_shedding_windows: str = ""
    random_grid_outages: bool = True
    grid_outage_rate_per_day: float = 0.08
    grid_outage_median_minutes: float = 25.0
    grid_voltage_upper_limit_v: float = 253.0
    grid_feeder_v_per_kw: float = 1.2

    # Defaults model a typical Indian residential net-metered connection;
    # onboarding overrides them (None = unknown, cost fields become null).
    primary_goal: str = "maximize_self_consumption"
    tariff_type: str | None = "Flat rate"
    tariff_rate_inr_per_kwh: float | None = 8.0
    export_credit_inr_per_kwh: float | None = 3.0
    meter_type: str | None = "Net meter"
    tou_peak_windows: str = "18:00-22:00"
    tou_peak_multiplier: float = 1.2
    tou_offpeak_windows: str = "22:00-06:00"
    tou_offpeak_multiplier: float = 0.8

    simulation_interval_seconds: int = 60
    random_seed: int = 42
    weather_refresh_minutes: int = 30
    weather_mode: WeatherMode = "live"
    scenario: str = "normal_day"
    output_file: str = "data/telemetry.jsonl"
    output_max_mb: float = 200.0
    output_backups: int = 3
    persist_state: bool = True
    state_dir: str = "data/state"
    speed: float = 1.0

    # Optional FastAPI HTTP ingest (dev/tests). Empty URL disables it.
    ingest_url: str = ""
    ingest_token: str = ""

    # Backend base URL for GET /energy/{id}/profile (onboarding → generator knobs).
    backend_url: str = ""
    from_onboarding: bool = False

    # RabbitMQ publisher (backend ingest pipeline). Empty URL disables it.
    rabbitmq_url: str = ""
    rabbitmq_exchange: str = "telemetry"
    rabbitmq_routing_key: str = "telemetry.ingest"
    rabbitmq_publish_timeout_seconds: float = 3.0

    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_forecast_days: int = 7
    open_meteo_past_days: int = 1
    open_meteo_timeout_seconds: float = 20.0

    energy_balance_tolerance_kw: float = 0.05
    schema_version: str = "1.1.0"

    device_catalog_json: str = ""

    @field_validator("simulation_interval_seconds")
    @classmethod
    def _interval_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("simulation_interval_seconds must be > 0")
        return value

    @field_validator("scenario")
    @classmethod
    def _known_scenario(cls, value: str) -> str:
        if value not in SCENARIOS:
            raise ValueError(f"unknown scenario {value!r}; choose one of {', '.join(SCENARIOS)}")
        return value

    @model_validator(mode="after")
    def _clamp_soc(self) -> SimulatorConfig:
        self.initial_battery_soc_percent = min(
            100.0, max(0.0, self.initial_battery_soc_percent)
        )
        self.battery_minimum_soc_percent = min(
            100.0, max(0.0, self.battery_minimum_soc_percent)
        )
        if self.battery_usable_capacity_kwh <= 0:
            self.battery_usable_capacity_kwh = self.battery_capacity_kwh
        else:
            self.battery_usable_capacity_kwh = min(
                self.battery_usable_capacity_kwh, self.battery_capacity_kwh
            )
        return self

    @property
    def interval_hours(self) -> float:
        return self.simulation_interval_seconds / 3600.0

    @property
    def household_seed(self) -> int:
        """Per-home randomness: habits, device timing, cloud shadows."""
        return self.random_seed ^ stable_hash(self.household_id)

    @property
    def location_seed(self) -> int:
        """Shared by homes in the same ~10 km cell: weather regimes, grid feeder outages."""
        return self.random_seed ^ stable_hash(f"{self.latitude:.1f},{self.longitude:.1f}")

    @property
    def tilt_deg(self) -> float:
        if self.panel_tilt_deg is not None:
            return min(90.0, max(0.0, self.panel_tilt_deg))
        return min(35.0, max(5.0, round(abs(self.latitude))))

    @property
    def azimuth_deg(self) -> float:
        if self.panel_azimuth_deg is not None:
            return self.panel_azimuth_deg % 360.0
        return 180.0 if self.latitude >= 0 else 0.0

    @property
    def synthetic_weather(self) -> bool:
        return self.weather_mode in ("fallback", "historical-style")

    def device_catalog(self) -> list[dict[str, Any]]:
        raw = self.device_catalog_json.strip()
        if not raw:
            return [dict(item) for item in DEFAULT_DEVICE_CATALOG]
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("DEVICE_CATALOG_JSON must be a JSON array")
        return parsed

    def output_path(self) -> Path:
        path = Path(self.output_file)
        if not path.is_absolute():
            path = SIMULATOR_DIR / path
        return path

    def state_path(self) -> Path:
        path = Path(self.state_dir)
        if not path.is_absolute():
            path = SIMULATOR_DIR / path
        return path / f"{self.household_id}.json"


def apply_scenario(config: SimulatorConfig, scenario: str) -> SimulatorConfig:
    """Return a copy of config with start-up scenario overrides applied.

    Day-level effects (weather, habits, grid events) are applied by the
    engines each tick so `auto` can switch scenario per simulated day.
    """
    updated = config.model_copy(deep=True)
    updated.scenario = scenario

    if scenario == "battery_low":
        updated.initial_battery_soc_percent = min(
            100.0, updated.battery_minimum_soc_percent + 2.0
        )
    elif scenario == "battery_full":
        updated.initial_battery_soc_percent = 100.0
    elif scenario == "battery_degraded":
        updated.battery_soh_percent = min(updated.battery_soh_percent, 72.0)
        updated.battery_charge_efficiency = min(updated.battery_charge_efficiency, 0.91)
        updated.battery_discharge_efficiency = min(updated.battery_discharge_efficiency, 0.91)
    elif scenario == "grid_outage":
        updated.force_grid_outage = True
    elif scenario == "dusty_panels":
        updated.initial_soiling_loss_percent = max(updated.initial_soiling_loss_percent, 18.0)
        updated.panel_cleaning_interval_days = 0
    return updated
