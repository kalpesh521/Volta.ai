"""
Pydantic models for weather, devices, energy flows, and telemetry.

`extra="allow"` is set on records that may grow later so adding a field in a
generator does not require every consumer to update immediately. To *remove*
a field, delete it here and stop populating it in the matching generator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtensibleModel(BaseModel):
    """Base for records that may gain optional keys over time."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# Open-Meteo hourly key → WeatherRecord field.
# Add a mapping here when you request a new weather variable.
WEATHER_FIELD_MAP: dict[str, str] = {
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "humidity_percent",
    "cloud_cover": "cloud_cover_percent",
    "precipitation": "precipitation_mm",
    "precipitation_probability": "precipitation_probability_percent",
    "wind_speed_10m": "wind_speed_kmh",
    "shortwave_radiation": "shortwave_radiation_wm2",
    "direct_radiation": "direct_radiation_wm2",
    "diffuse_radiation": "diffuse_radiation_wm2",
    "weather_code": "weather_code",
}


class WeatherRecord(ExtensibleModel):
    timestamp: datetime
    temperature_c: float
    humidity_percent: float = Field(ge=0, le=100)
    cloud_cover_percent: float = Field(ge=0, le=100)
    precipitation_mm: float = Field(ge=0)
    precipitation_probability_percent: float = Field(ge=0, le=100)
    wind_speed_kmh: float = Field(ge=0)
    shortwave_radiation_wm2: float = Field(ge=0)
    direct_radiation_wm2: float = Field(ge=0)
    diffuse_radiation_wm2: float = Field(ge=0)
    weather_code: int | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    source: str = "open-meteo"
    data_quality: str = "simulated"

    @field_validator(
        "shortwave_radiation_wm2",
        "direct_radiation_wm2",
        "diffuse_radiation_wm2",
        "precipitation_mm",
        "wind_speed_kmh",
        mode="before",
    )
    @classmethod
    def _never_negative(cls, value: float | None) -> float:
        if value is None:
            return 0.0
        return max(0.0, float(value))

    @field_validator(
        "humidity_percent",
        "cloud_cover_percent",
        "precipitation_probability_percent",
        mode="before",
    )
    @classmethod
    def _clamp_percent(cls, value: float | None) -> float:
        if value is None:
            return 0.0
        return min(100.0, max(0.0, float(value)))


class LocationRecord(ExtensibleModel):
    name: str
    latitude: float
    longitude: float
    timezone: str
    country: str | None = None
    country_code: str | None = None
    admin1: str | None = None
    admin2: str | None = None
    elevation_m: float | None = None
    population: int | None = None
    location_id: int | None = None
    source: str = "open-meteo-geocoding"
    data_quality: str = "resolved"
    query: str | None = None

    def label(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)


def default_location() -> LocationRecord:
    return LocationRecord(
        name="Pimpri-Chinchwad",
        latitude=18.6298,
        longitude=73.7997,
        timezone="Asia/Kolkata",
        country="India",
        country_code="IN",
        admin1="Maharashtra",
        elevation_m=570.0,
        source="default",
        data_quality="fallback",
        query="Pimpri-Chinchwad",
    )


class DeviceReading(ExtensibleModel):
    device_id: str
    device_name: str
    device_type: str
    rated_power_kw: float
    current_state: str
    current_power_kw: float = Field(ge=0)
    energy_interval_kwh: float = Field(ge=0)
    critical: bool
    controllable: bool


class EnergyFlows(ExtensibleModel):
    solar_to_home_kw: float = Field(ge=0)
    solar_to_battery_kw: float = Field(ge=0)
    solar_to_grid_kw: float = Field(ge=0)
    battery_to_home_kw: float = Field(ge=0)
    grid_to_home_kw: float = Field(ge=0)
    unserved_load_kw: float = Field(ge=0)
    solar_curtailed_kw: float = Field(ge=0, default=0.0)
    grid_to_battery_kw: float = Field(ge=0, default=0.0)


class EnergyBalanceResult(ExtensibleModel):
    energy_balance_status: str
    energy_balance_error_kw: float
    energy_balance_valid: bool
    system_losses_kw: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class TelemetryRecord(ExtensibleModel):
    timestamp: datetime
    household_id: str
    data_source: str
    data_quality: str

    solar_power_kw: float
    solar_energy_interval_kwh: float
    solar_energy_today_kwh: float
    solar_energy_total_kwh: float

    home_load_power_kw: float
    home_consumption_interval_kwh: float
    home_consumption_today_kwh: float
    home_consumption_total_kwh: float

    battery_soc_percent: float
    battery_soh_percent: float
    battery_charge_power_kw: float
    battery_discharge_power_kw: float
    battery_energy_available_kwh: float
    battery_status: str

    grid_status: str
    grid_import_power_kw: float
    grid_export_power_kw: float
    total_import_kwh: float
    total_export_kwh: float

    solar_to_home_kw: float
    solar_to_battery_kw: float
    solar_to_grid_kw: float
    battery_to_home_kw: float
    grid_to_home_kw: float
    unserved_load_kw: float

    energy_balance_status: str
    energy_balance_error_kw: float
    energy_balance_valid: bool = True

    devices: list[dict[str, Any]] = Field(default_factory=list)
    weather: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _non_negative_powers(self) -> TelemetryRecord:
        power_fields = (
            "solar_power_kw",
            "home_load_power_kw",
            "battery_charge_power_kw",
            "battery_discharge_power_kw",
            "grid_import_power_kw",
            "grid_export_power_kw",
            "solar_to_home_kw",
            "solar_to_battery_kw",
            "solar_to_grid_kw",
            "battery_to_home_kw",
            "grid_to_home_kw",
            "unserved_load_kw",
        )
        for name in power_fields:
            value = getattr(self, name)
            if value < -1e-9:
                raise ValueError(f"{name} cannot be negative")
            if value < 0:
                setattr(self, name, 0.0)
        return self
