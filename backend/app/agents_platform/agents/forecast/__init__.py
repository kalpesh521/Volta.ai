"""
Forecast engine: hourly solar and load forecast with prediction intervals (pure, no DB, no network
in the core). The agent layer maps telemetry to `HistoryPoint`, picks a `WeatherForecastPort`, calls
`forecast(...)`, and feeds `ForecastResult.to_facts()` to the answer prompt.
"""
from .engine import forecast
from .models import (
    DEFAULT_HORIZON_HOURS,
    MAX_HORIZON_HOURS,
    ForecastInputs,
    ForecastWeatherError,
    HistoryPoint,
    WeatherForecastPort,
    WeatherHour,
    WeatherSeries,
)
from .result import ForecastResult, ForecastSlot
from .weather import OpenMeteoWeather, PersistenceWeather

__all__ = [
    "DEFAULT_HORIZON_HOURS",
    "MAX_HORIZON_HOURS",
    "ForecastInputs",
    "ForecastResult",
    "ForecastSlot",
    "ForecastWeatherError",
    "HistoryPoint",
    "OpenMeteoWeather",
    "PersistenceWeather",
    "WeatherForecastPort",
    "WeatherHour",
    "WeatherSeries",
    "forecast",
]
