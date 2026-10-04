"""
Read-only LangChain tools over the existing energy APIs.

Tools are bound to one household through the gateway closure. `household_id`
is deliberately not an argument, so neither the planner nor a future
tool-calling LLM can address another tenant's data.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import datetime as dt
from enum import StrEnum
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.modules.assistant.domain.context import (
    battery_facts,
    daily_facts,
    devices_facts,
    grid_facts,
    hourly_facts,
    household_facts,
    live_facts,
    trend_facts,
    weather_facts,
)
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.energy.schemas import DevicesOut, WeatherOut


class ToolName(StrEnum):
    LIVE_ENERGY_STATE = "get_live_energy_state"
    DAILY_ENERGY_SUMMARY = "get_daily_energy_summary"
    HOURLY_ENERGY_SUMMARY = "get_hourly_energy_summary"
    BATTERY_STATUS = "get_battery_status"
    GRID_STATUS = "get_grid_status"
    DEVICE_READINGS = "get_device_readings"
    WEATHER_DATA = "get_weather_data"
    RECENT_TREND = "get_recent_trend"
    HOUSEHOLD_PROFILE = "get_household_profile"


class NoArgs(BaseModel):
    pass


class DayArgs(BaseModel):
    date: dt.date | None = Field(
        default=None,
        description="Calendar date (YYYY-MM-DD) in the household timezone. Omit for the latest day.",
    )


ToolResult = dict[str, Any]

_NO_TELEMETRY = "No telemetry has been received for this home yet."


def unavailable(reason: str) -> ToolResult:
    return {"available": False, "reason": reason}


def is_available(result: ToolResult | None) -> bool:
    return bool(result) and result.get("available", True) is not False


def build_energy_tools(
    gateway: EnergyDataGateway,
    *,
    trend_window_minutes: int = 60,
    trend_history_limit: int = 240,
) -> dict[ToolName, BaseTool]:
    async def get_live_energy_state() -> ToolResult:
        live = await gateway.live()
        if live is None:
            return unavailable(_NO_TELEMETRY)
        return live_facts(live).model_dump(mode="json")

    async def get_battery_status() -> ToolResult:
        live = await gateway.live()
        if live is None:
            return unavailable(_NO_TELEMETRY)
        return battery_facts(live.battery, live.timestamp).model_dump(mode="json")

    async def get_grid_status() -> ToolResult:
        live = await gateway.live()
        if live is None:
            return unavailable(_NO_TELEMETRY)
        return grid_facts(live.grid, live.timestamp).model_dump(mode="json")

    async def get_device_readings() -> ToolResult:
        live = await gateway.live()
        if live is None:
            return unavailable(_NO_TELEMETRY)
        out = DevicesOut(household_id=live.household_id, timestamp=live.timestamp, devices=live.devices)
        return devices_facts(out).model_dump(mode="json")

    async def get_weather_data() -> ToolResult:
        live = await gateway.live()
        if live is None:
            return unavailable(_NO_TELEMETRY)
        facts = weather_facts(
            WeatherOut(household_id=live.household_id, timestamp=live.timestamp, weather=live.weather)
        )
        if facts is None:
            return unavailable("The latest reading has no weather data.")
        return facts.model_dump(mode="json")

    async def get_daily_energy_summary(date: dt.date | None = None) -> ToolResult:
        daily = await gateway.daily(date)
        if daily is None:
            return unavailable(_NO_TELEMETRY)
        return daily_facts(daily).model_dump(mode="json")

    async def get_hourly_energy_summary(date: dt.date | None = None) -> ToolResult:
        hourly = await gateway.hourly(date)
        if hourly is None:
            return unavailable(_NO_TELEMETRY)
        return hourly_facts(hourly).model_dump(mode="json")

    async def get_recent_trend() -> ToolResult:
        records = await gateway.history(trend_history_limit)
        if not records:
            return unavailable(_NO_TELEMETRY)
        facts = trend_facts(records, window_minutes=trend_window_minutes)
        if facts is None:
            return unavailable("Not enough recent readings to show a trend yet.")
        return facts.model_dump(mode="json")

    async def get_household_profile() -> ToolResult:
        return household_facts(gateway.profile(), gateway.onboarding_details()).model_dump(mode="json")

    specs: tuple[tuple[ToolName, Callable[..., Awaitable[ToolResult]], type[BaseModel], str], ...] = (
        (
            ToolName.LIVE_ENERGY_STATE,
            get_live_energy_state,
            NoArgs,
            "Latest live snapshot: solar and home load in kW, power flows, today's kWh so far, data quality.",
        ),
        (
            ToolName.BATTERY_STATUS,
            get_battery_status,
            NoArgs,
            "Latest battery state of charge, health, charge/discharge power, temperature and faults.",
        ),
        (
            ToolName.GRID_STATUS,
            get_grid_status,
            NoArgs,
            "Latest grid availability, import and export power, voltage and frequency.",
        ),
        (
            ToolName.DEVICE_READINGS,
            get_device_readings,
            NoArgs,
            "Latest per-appliance state and power draw, sorted by highest consumption.",
        ),
        (
            ToolName.WEATHER_DATA,
            get_weather_data,
            NoArgs,
            "Weather used for the latest reading: temperature, cloud cover, solar radiation, sunrise/sunset.",
        ),
        (
            ToolName.DAILY_ENERGY_SUMMARY,
            get_daily_energy_summary,
            DayArgs,
            "Daily kWh totals: solar generated, home consumption, grid import/export, battery, peak load, savings.",
        ),
        (
            ToolName.HOURLY_ENERGY_SUMMARY,
            get_hourly_energy_summary,
            DayArgs,
            "Hourly kWh buckets for one day with peak solar, consumption and import hours.",
        ),
        (
            ToolName.RECENT_TREND,
            get_recent_trend,
            NoArgs,
            "How solar, load, battery SOC and grid exchange changed over the last hour of readings.",
        ),
        (
            ToolName.HOUSEHOLD_PROFILE,
            get_household_profile,
            NoArgs,
            "Home setup entered at onboarding: location, panels, inverter, battery, grid meter, DISCOM, "
            "sanctioned load, tariff, monthly bill, goal and tracked appliances.",
        ),
    )
    return {
        name: StructuredTool.from_function(
            coroutine=fn,
            name=name.value,
            description=description,
            args_schema=args_schema,
        )
        for name, fn, args_schema, description in specs
    }
