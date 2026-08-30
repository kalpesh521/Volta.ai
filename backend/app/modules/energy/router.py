"""
Energy HTTP endpoints.

Write path (simulator):
  POST /energy/ingest                 X-Ingest-Token  (not a user JWT)

Read path (owner JWT; household_id must belong to the caller):
  GET  /energy/me/homes
  GET  /energy/me/live|daily|hourly|history|profile   (?household_id= optional)
  GET  /energy/onboarding-profiles                  X-Ingest-Token
  GET  /energy/{household_id}/live|daily|hourly|history|...
  GET  /energy/{household_id}/profile   ingest token OR owner JWT

All user GET routes require Authorization: Bearer <access_token>
and a completed onboarding row for that household.

Do not add `from __future__ import annotations` in this file. slowapi wraps
ingest and cannot resolve postponed annotations, which makes FastAPI treat
the TelemetryRecord body as a query parameter.
"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.energy.access import (
    require_my_completed_system,
    require_owned_household,
    require_profile_access,
)
from app.modules.energy.deps import get_energy_service, require_ingest_token
from app.modules.energy.profile import build_simulator_profile
from app.modules.onboarding.deps import get_onboarding_repository, get_onboarding_service
from app.modules.onboarding.repository import OnboardingRepository
from app.modules.onboarding.schemas import HomeListOut
from app.modules.onboarding.service import OnboardingService
from app.modules.energy.schemas import (
    BatteryStatusOut,
    DailySummaryOut,
    DevicesOut,
    GridStatusOut,
    HistoryOut,
    HourlySummaryOut,
    LiveEnergyOut,
    SimulatorProfileListOut,
    SimulatorProfileOut,
    TelemetryRecord,
    WeatherOut,
)
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem

router = APIRouter(prefix="/energy", tags=["energy"])


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
    if not settings.INGEST_HTTP_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await service.ingest(payload)


# ── Caller's own household (register before /{household_id} so "me" is not captured)

@router.get(
    "/onboarding-profiles",
    response_model=SimulatorProfileListOut,
    summary="All completed homes (simulator; X-Ingest-Token)",
)
async def list_onboarding_profiles(
    _: None = Depends(require_ingest_token),
    repo: OnboardingRepository = Depends(get_onboarding_repository),
) -> SimulatorProfileListOut:
    systems = await repo.get_completed_systems()
    return SimulatorProfileListOut(
        profiles=[build_simulator_profile(system) for system in systems]
    )


@router.get(
    "/me/homes",
    response_model=HomeListOut,
    summary="Homes owned by the login token (primary is used by /energy/me/*)",
)
async def list_my_homes(
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> HomeListOut:
    return await service.list_homes(current_user.id)


@router.get(
    "/me/live",
    response_model=LiveEnergyOut,
    summary="Live energy snapshot for the authenticated user's household",
)
async def get_my_live_energy(
    system: SolarSystem = Depends(require_my_completed_system),
    service: EnergyService = Depends(get_energy_service),
) -> LiveEnergyOut:
    return await service.get_live(system.household_id, system.location)


@router.get(
    "/me/daily",
    response_model=DailySummaryOut,
    summary="Daily kWh totals for the authenticated user's household",
)
async def get_my_daily_summary(
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    system: SolarSystem = Depends(require_my_completed_system),
    service: EnergyService = Depends(get_energy_service),
) -> DailySummaryOut:
    return await service.get_daily(system.household_id, day=date_filter)


@router.get(
    "/me/hourly",
    response_model=HourlySummaryOut,
    summary="Hourly kWh buckets for the authenticated user's household",
)
async def get_my_hourly_summary(
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    system: SolarSystem = Depends(require_my_completed_system),
    service: EnergyService = Depends(get_energy_service),
) -> HourlySummaryOut:
    return await service.get_hourly(system.household_id, day=date_filter)


@router.get(
    "/me/history",
    response_model=HistoryOut,
    summary="Recent telemetry ticks for the authenticated user's household",
)
async def get_my_history(
    limit: int = Query(default=120, ge=1, le=2000),
    system: SolarSystem = Depends(require_my_completed_system),
    service: EnergyService = Depends(get_energy_service),
) -> HistoryOut:
    return await service.get_history(system.household_id, limit=limit)


@router.get(
    "/me/profile",
    response_model=SimulatorProfileOut,
    summary="Simulator knobs derived from the caller's completed onboarding",
)
async def get_my_simulator_profile(
    system: SolarSystem = Depends(require_my_completed_system),
) -> SimulatorProfileOut:
    return build_simulator_profile(system)


@router.get(
    "/{household_id}/live",
    response_model=LiveEnergyOut,
    summary="Live energy snapshot (solar, load, battery, grid, devices, weather)",
)
async def get_live_energy(
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> LiveEnergyOut:
    return await service.get_live(system.household_id, system.location)


@router.get(
    "/{household_id}/battery",
    response_model=BatteryStatusOut,
    summary="Latest battery status",
)
async def get_battery(
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> BatteryStatusOut:
    return await service.get_battery(system.household_id)


@router.get(
    "/{household_id}/grid",
    response_model=GridStatusOut,
    summary="Latest grid status",
)
async def get_grid(
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> GridStatusOut:
    return await service.get_grid(system.household_id)


@router.get(
    "/{household_id}/devices",
    response_model=DevicesOut,
    summary="Latest appliance readings",
)
async def get_devices(
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> DevicesOut:
    return await service.get_devices(system.household_id)


@router.get(
    "/{household_id}/weather",
    response_model=WeatherOut,
    summary="Weather used for the latest tick",
)
async def get_weather(
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> WeatherOut:
    return await service.get_weather(system.household_id)


@router.get(
    "/{household_id}/history",
    response_model=HistoryOut,
    summary="Recent telemetry ticks (ring buffer)",
)
async def get_history(
    limit: int = Query(default=120, ge=1, le=2000),
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> HistoryOut:
    return await service.get_history(system.household_id, limit=limit)


@router.get(
    "/{household_id}/daily",
    response_model=DailySummaryOut,
    summary="Daily kWh totals from ingested ticks",
)
async def get_daily_summary(
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> DailySummaryOut:
    return await service.get_daily(system.household_id, day=date_filter)


@router.get(
    "/{household_id}/hourly",
    response_model=HourlySummaryOut,
    summary="Hourly kWh buckets for one calendar day",
)
async def get_hourly_summary(
    date_filter: date | None = Query(
        default=None,
        alias="date",
        description="Calendar date in the household timezone. Defaults to the latest tick's date.",
    ),
    system: SolarSystem = Depends(require_owned_household),
    service: EnergyService = Depends(get_energy_service),
) -> HourlySummaryOut:
    return await service.get_hourly(system.household_id, day=date_filter)


@router.get(
    "/{household_id}/profile",
    response_model=SimulatorProfileOut,
    summary="Simulator knobs for this household (ingest token or owner JWT)",
)
async def get_simulator_profile(
    system: SolarSystem = Depends(require_profile_access),
) -> SimulatorProfileOut:
    return build_simulator_profile(system)
