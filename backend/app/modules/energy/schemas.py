"""
Pydantic contracts for telemetry ingest and dashboard read APIs.

Field names match `simulator/models.py` so a TelemetryRecord JSON line can be
POSTed unchanged. Extra keys (scenario, location, warnings, …) are kept so
the generator can grow without a backend deploy.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtensibleModel(BaseModel):
    """Records that may gain optional keys over time."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


_HOUSEHOLD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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

    @field_validator("timestamp", "sunrise", "sunset", mode="before")
    @classmethod
    def _parse_dt(cls, value: datetime | str | None) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return _aware(value)
        return value

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


class DeviceReading(ExtensibleModel):
    device_id: str = Field(min_length=1, max_length=64)
    device_name: str = Field(min_length=1, max_length=120)
    device_type: str = Field(min_length=1, max_length=64)
    rated_power_kw: float = Field(ge=0)
    current_state: str = Field(min_length=1, max_length=32)
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


class BatteryStatusOut(BaseModel):
    soc_percent: float
    soh_percent: float
    charge_power_kw: float
    discharge_power_kw: float
    energy_available_kwh: float
    status: str
    temperature_c: float | None = None
    fault_code: str | None = None


class GridStatusOut(BaseModel):
    status: str
    import_power_kw: float
    export_power_kw: float
    total_import_kwh: float
    total_export_kwh: float
    voltage_v: float | None = None
    frequency_hz: float | None = None


class SolarSnapshotOut(BaseModel):
    power_kw: float
    energy_today_kwh: float
    energy_total_kwh: float
    energy_interval_kwh: float


class LoadSnapshotOut(BaseModel):
    power_kw: float
    consumption_today_kwh: float
    consumption_total_kwh: float
    consumption_interval_kwh: float


class TelemetryRecord(ExtensibleModel):
    """One simulator tick. Extra keys are preserved."""

    timestamp: datetime
    household_id: str = Field(pattern=_HOUSEHOLD_ID_PATTERN)
    data_source: str = Field(min_length=1, max_length=32)
    data_quality: str = Field(min_length=1, max_length=32)

    solar_power_kw: float = Field(ge=0)
    solar_energy_interval_kwh: float = Field(ge=0)
    solar_energy_today_kwh: float = Field(ge=0)
    solar_energy_total_kwh: float = Field(ge=0)

    home_load_power_kw: float = Field(ge=0)
    home_consumption_interval_kwh: float = Field(ge=0)
    home_consumption_today_kwh: float = Field(ge=0)
    home_consumption_total_kwh: float = Field(ge=0)

    battery_soc_percent: float
    battery_soh_percent: float
    battery_charge_power_kw: float = Field(ge=0)
    battery_discharge_power_kw: float = Field(ge=0)
    battery_energy_available_kwh: float = Field(ge=0)
    battery_status: str = Field(min_length=1, max_length=32)

    grid_status: str = Field(min_length=1, max_length=32)
    grid_import_power_kw: float = Field(ge=0)
    grid_export_power_kw: float = Field(ge=0)
    total_import_kwh: float = Field(ge=0)
    total_export_kwh: float = Field(ge=0)

    solar_to_home_kw: float = Field(ge=0)
    solar_to_battery_kw: float = Field(ge=0)
    solar_to_grid_kw: float = Field(ge=0)
    battery_to_home_kw: float = Field(ge=0)
    grid_to_home_kw: float = Field(ge=0)
    unserved_load_kw: float = Field(ge=0)

    energy_balance_status: str = "valid"
    energy_balance_error_kw: float = 0.0
    energy_balance_valid: bool = True

    devices: list[DeviceReading] = Field(default_factory=list)
    weather: WeatherRecord | None = None

    # Interval energy — filled on ingest if the producer omitted them.
    grid_import_interval_kwh: float = Field(default=0.0, ge=0)
    grid_export_interval_kwh: float = Field(default=0.0, ge=0)
    battery_charge_interval_kwh: float = Field(default=0.0, ge=0)
    battery_discharge_interval_kwh: float = Field(default=0.0, ge=0)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _parse_timestamp(cls, value: datetime | str) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return _aware(value)
        raise TypeError("timestamp must be a datetime")

    @field_validator("battery_soc_percent", "battery_soh_percent", mode="before")
    @classmethod
    def _clamp_battery_percent(cls, value: float | None) -> float:
        if value is None:
            return 0.0
        return min(100.0, max(0.0, float(value)))

    @field_validator("weather", mode="before")
    @classmethod
    def _empty_weather(cls, value: Any) -> Any:
        if value in (None, {}, ""):
            return None
        return value

    @model_validator(mode="after")
    def _power_invariants(self) -> TelemetryRecord:
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

        if self.grid_import_power_kw > 0.02 and self.grid_export_power_kw > 0.02:
            raise ValueError("grid import and export cannot both be positive in one interval")
        if self.battery_charge_power_kw > 0.02 and self.battery_discharge_power_kw > 0.02:
            raise ValueError("battery charge and discharge cannot both be positive in one interval")
        return self


class LiveEnergyOut(BaseModel):
    household_id: str
    timestamp: datetime
    data_source: str
    data_quality: str
    solar: SolarSnapshotOut
    load: LoadSnapshotOut
    battery: BatteryStatusOut
    grid: GridStatusOut
    flows: EnergyFlows
    devices: list[DeviceReading]
    weather: WeatherRecord | None
    energy_balance_status: str
    energy_balance_error_kw: float
    energy_balance_valid: bool
    scenario: str | None = None
    warnings: list[str] = Field(default_factory=list)


class HistoryOut(BaseModel):
    household_id: str
    count: int
    records: list[TelemetryRecord]


class DevicesOut(BaseModel):
    household_id: str
    timestamp: datetime
    devices: list[DeviceReading]


class WeatherOut(BaseModel):
    household_id: str
    timestamp: datetime
    weather: WeatherRecord | None


class EnergyTotals(BaseModel):
    solar_generation_kwh: float
    home_consumption_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_charge_kwh: float
    battery_discharge_kwh: float


class DailySummaryOut(EnergyTotals):
    household_id: str
    period: str = "daily"
    date: date
    timezone: str
    reading_count: int
    period_start: datetime | None = None
    period_end: datetime | None = None


class HourlyBucketOut(EnergyTotals):
    hour_start: datetime
    reading_count: int


class HourlySummaryOut(BaseModel):
    household_id: str
    period: str = "hourly"
    date: date
    timezone: str
    buckets: list[HourlyBucketOut]
