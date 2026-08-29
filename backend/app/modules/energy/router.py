"""
Energy HTTP endpoints.

Write path (simulator):
  POST /energy/ingest                 X-Ingest-Token  (not a user JWT)

Read path (dashboard / future AI tools):
  GET  /energy/{household_id}/live
  GET  /energy/{household_id}/battery
  GET  /energy/{household_id}/grid
  GET  /energy/{household_id}/devices
  GET  /energy/{household_id}/weather
  GET  /energy/{household_id}/history
  GET  /energy/{household_id}/daily
  GET  /energy/{household_id}/hourly

All GET routes require Authorization: Bearer <access_token>.

Do not add `from __future__ import annotations` in this file. slowapi wraps
ingest and cannot resolve postponed annotations, which makes FastAPI treat
the TelemetryRecord body as a query parameter.
"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query, Request, status

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.energy.deps import get_energy_service, require_ingest_token
from app.modules.energy.schemas import (
    BatteryStatusOut,
    DailySummaryOut,
    DevicesOut,
    GridStatusOut,
    HistoryOut,
    HourlySummaryOut,
    LiveEnergyOut,
    TelemetryRecord,
    WeatherOut,
)
from app.modules.energy.service import EnergyService

router = APIRouter(prefix="/energy", tags=["energy"])

HouseholdId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        description="Simulator household_id, e.g. home_001",
    ),
]


@router.post(
    "/ingest",
    response_model=TelemetryRecord,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest one telemetry tick from the simulator",
)
@limiter.limit(settings.RATE_LIMIT_INGEST)
async def ingest_telemetry(
    request: Request,
    payload: Annotated[TelemetryRecord, Body()],
    _: None = Depends(require_ingest_token),
    service: EnergyService = Depends(get_energy_service),
) -> TelemetryRecord:
    """
    Validates ranges and power invariants, derives interval kWh, recomputes
    the energy-balance check, then stores the latest snapshot + history row.

    Returns **202** with the normalized record. Invalid payloads return **400**.
    Missing/wrong `X-Ingest-Token` returns **401**.
    """
    return await service.ingest(payload)


@router.get(
    "/{household_id}/live",
    response_model=LiveEnergyOut,
    summary="Live energy snapshot (solar, load, battery, grid, devices, weather)",
)
async def get_live_energy(
    household_id: HouseholdId,
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> LiveEnergyOut:
    return await service.get_live(household_id)


@router.get(
    "/{household_id}/battery",
    response_model=BatteryStatusOut,
    summary="Latest battery status",
)
async def get_battery(
    household_id: HouseholdId,
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> BatteryStatusOut:
    return await service.get_battery(household_id)


@router.get(
    "/{household_id}/grid",
    response_model=GridStatusOut,
    summary="Latest grid status",
)
async def get_grid(
    household_id: HouseholdId,
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> GridStatusOut:
    return await service.get_grid(household_id)


@router.get(
    "/{household_id}/devices",
    response_model=DevicesOut,
    summary="Latest appliance readings",
)
async def get_devices(
    household_id: HouseholdId,
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> DevicesOut:
    return await service.get_devices(household_id)


@router.get(
    "/{household_id}/weather",
    response_model=WeatherOut,
    summary="Weather used for the latest tick",
)
async def get_weather(
    household_id: HouseholdId,
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> WeatherOut:
    return await service.get_weather(household_id)


@router.get(
    "/{household_id}/history",
    response_model=HistoryOut,
    summary="Recent telemetry ticks (ring buffer)",
)
async def get_history(
    household_id: HouseholdId,
    limit: int = Query(default=120, ge=1, le=2000),
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> HistoryOut:
    return await service.get_history(household_id, limit=limit)


@router.get(
    "/{household_id}/daily",
    response_model=DailySummaryOut,
    summary="Daily kWh totals from ingested ticks",
)
async def get_daily_summary(
    household_id: HouseholdId,
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> DailySummaryOut:
    return await service.get_daily(household_id, day=date_filter)


@router.get(
    "/{household_id}/hourly",
    response_model=HourlySummaryOut,
    summary="Hourly kWh buckets for one calendar day",
)
async def get_hourly_summary(
    household_id: HouseholdId,
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    _user: User = Depends(get_current_user),
    service: EnergyService = Depends(get_energy_service),
) -> HourlySummaryOut:
    return await service.get_hourly(household_id, day=date_filter)
