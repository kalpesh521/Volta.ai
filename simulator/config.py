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

SIMULATOR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SIMULATOR_DIR.parent

load_dotenv(SIMULATOR_DIR / ".env")
load_dotenv()

WeatherMode = Literal["live", "fallback", "historical-style"]
ScenarioName = Literal[
    "normal_day",
    "cloudy_day",
    "rainy_day",
    "battery_low",
    "battery_full",
    "grid_outage",
    "high_evening_load",
    "sensor_failure",
]

SCENARIOS: tuple[str, ...] = (
    "normal_day",
    "cloudy_day",
    "rainy_day",
    "battery_low",
    "battery_full",
    "grid_outage",
    "high_evening_load",
    "sensor_failure",
)

WEATHER_MODES: tuple[str, ...] = ("live", "fallback", "historical-style")

# Open-Meteo hourly keys. Add or remove entries here AND update
# `WEATHER_FIELD_MAP` in models.py if the telemetry weather object should change.
OPEN_METEO_HOURLY_FIELDS: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "precipitation_probability",
    "cloud_cover",
    "wind_speed_10m",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
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

    solar_capacity_kwp: float = 5.0
    inverter_capacity_kw: float = 5.0
    solar_efficiency: float = 0.82
    shading_factor: float = 0.95
    pv_temp_coefficient_per_c: float = 0.004

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

    grid_available: bool = True
    zero_export_mode: bool = False
    zero_export_limit_kw: float = 0.0
    grid_nominal_voltage_v: float = 230.0
    grid_nominal_frequency_hz: float = 50.0
    grid_outage_window: str = ""

    simulation_interval_seconds: int = 60
    random_seed: int = 42
    weather_refresh_minutes: int = 30
    weather_mode: WeatherMode = "live"
    scenario: ScenarioName = "normal_day"
    output_file: str = "data/telemetry.jsonl"
    speed: float = 1.0

    # Optional FastAPI HTTP ingest (dev/tests). Empty URL disables it.
    ingest_url: str = ""
    ingest_token: str = ""

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
    schema_version: str = "1.0.0"

    device_catalog_json: str = ""

    @field_validator("simulation_interval_seconds")
    @classmethod
    def _interval_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("simulation_interval_seconds must be > 0")
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


def apply_scenario(config: SimulatorConfig, scenario: str) -> SimulatorConfig:
    """Return a copy of config with scenario-specific overrides applied."""
    updated = config.model_copy(deep=True)
    updated.scenario = scenario  # type: ignore[assignment]

    if scenario == "battery_low":
        updated.initial_battery_soc_percent = min(
            100.0, updated.battery_minimum_soc_percent + 2.0
        )
    elif scenario == "battery_full":
        updated.initial_battery_soc_percent = 100.0
    elif scenario == "grid_outage":
        updated.grid_available = False
    return updated
