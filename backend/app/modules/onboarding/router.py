"""
Onboarding HTTP endpoints.

All routes require a valid access token (get_current_user dependency).
Conditional steps:
  PUT /onboarding/battery  → 422 if system_type is On-grid
  PUT /onboarding/grid     → 422 if system_type is Off-grid

Route summary
─────────────
  PUT  /onboarding/system          Save / update system + inverter info (step 1+2)
  PUT  /onboarding/battery         Save / update battery config          (step 3 — Off-grid/Hybrid)
  PUT  /onboarding/grid            Save / update grid config             (step 4 — On-grid/Hybrid)
  PUT  /onboarding/appliances      Save / update appliance selection     (step 5)
  POST /onboarding/complete        Mark onboarding as finished           (from review screen)
  GET  /onboarding/status          Current step + completion state
  GET  /onboarding/summary         Full data dump for the review screen
"""
from fastapi import APIRouter, Depends, status

from app.core.deps import get_current_user
from app.models.user import User
from app.modules.onboarding.deps import get_onboarding_service
from app.modules.onboarding.schemas import (
    AppliancesStepIn,
    BatteryStepIn,
    GridStepIn,
    OnboardingStatusOut,
    OnboardingSummaryOut,
    SolarSystemOut,
    SystemStepIn,
)
from app.modules.onboarding.service import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ── Step 1+2: System + Inverter ───────────────────────────────────────────────

@router.put(
    "/system",
    response_model=SolarSystemOut,
    summary="Save system & inverter info (steps 1+2)",
)
async def save_system(
    data: SystemStepIn,
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> SolarSystemOut:
    """
    Upsert solar panel and inverter information.

    **All system types** must complete this step first.
    Re-submitting with a changed `system_type` automatically removes stale
    battery/grid records (e.g. switching On-grid → Hybrid re-requires battery).

    Returns the updated `SolarSystem` record.
    """
    system = await service.save_system(current_user.id, data)
    return SolarSystemOut.model_validate(system)


# ── Step 3: Battery ───────────────────────────────────────────────────────────

@router.put(
    "/battery",
    response_model=SolarSystemOut,
    summary="Save battery config (step 3 — Off-grid / Hybrid only)",
)
async def save_battery(
    data: BatteryStepIn,
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> SolarSystemOut:
    """
    Upsert battery & backup configuration.

    **Only valid** when `system_type` is `Off-grid` or `Hybrid`.
    Returns **422** with `code: system_type_conflict` for `On-grid` systems.

    | Field                 | Constraint       |
    |-----------------------|------------------|
    | battery_capacity_kwh  | ≥ 1 kWh          |
    | backup_hours          | 2, 4, or 8 hours |
    | reserve_pct           | 5 – 50 %         |
    """
    system = await service.save_battery(current_user.id, data)
    return SolarSystemOut.model_validate(system)


# ── Step 4: Grid ──────────────────────────────────────────────────────────────

@router.put(
    "/grid",
    response_model=SolarSystemOut,
    summary="Save grid & tariff config (step 4 — On-grid / Hybrid only)",
)
async def save_grid(
    data: GridStepIn,
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> SolarSystemOut:
    """
    Upsert grid connection and tariff information.

    **Only valid** when `system_type` is `On-grid` or `Hybrid`.
    Returns **422** with `code: system_type_conflict` for `Off-grid` systems.

    | Field              | Constraint          |
    |--------------------|---------------------|
    | sanctioned_load_kw | ≥ 1 kW              |
    | discom             | optional (max 100 chars) |
    """
    system = await service.save_grid(current_user.id, data)
    return SolarSystemOut.model_validate(system)


# ── Step 5: Appliances ────────────────────────────────────────────────────────

@router.put(
    "/appliances",
    response_model=SolarSystemOut,
    summary="Save appliance selection (step 5 — all system types)",
)
async def save_appliances(
    data: AppliancesStepIn,
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> SolarSystemOut:
    """
    Replace the tracked-appliance list wholesale.

    Valid `appliance_key` values: `wash`, `heater`, `ev`, `pump`, `ac`, `fridge`.
    Setting `is_critical: true` means Suryaa will never auto-off that device.
    An empty `appliances` list is accepted (user can deselect everything).
    No duplicate `appliance_key` entries allowed in a single request.
    """
    system = await service.save_appliances(current_user.id, data)
    return SolarSystemOut.model_validate(system)


# ── Complete ──────────────────────────────────────────────────────────────────

@router.post(
    "/complete",
    response_model=OnboardingStatusOut,
    status_code=status.HTTP_200_OK,
    summary="Mark onboarding as complete (review screen — all system types)",
)
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> OnboardingStatusOut:
    """
    Finalise onboarding and unlock the main dashboard.

    Returns **403** listing any missing required steps.
    Returns **409** if onboarding is already complete.

    Required steps by system type:
    | System type | Required steps                          |
    |-------------|------------------------------------------|
    | On-grid     | system → grid → appliances               |
    | Off-grid    | system → battery → appliances            |
    | Hybrid      | system → battery → grid → appliances     |
    """
    await service.complete_onboarding(current_user.id)
    return await service.get_status(current_user.id)


# ── Read ──────────────────────────────────────────────────────────────────────

@router.get(
    "/status",
    response_model=OnboardingStatusOut,
    summary="Get onboarding progress (all system types)",
)
async def get_onboarding_status(
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> OnboardingStatusOut:
    """
    Returns the current onboarding state so the frontend can resume from the
    correct step after a page reload.

    `steps_remaining` is dynamically computed from the user's chosen `system_type`.
    """
    return await service.get_status(current_user.id)


@router.get(
    "/summary",
    response_model=OnboardingSummaryOut,
    summary="Get full onboarding data (review screen)",
)
async def get_onboarding_summary(
    current_user: User = Depends(get_current_user),
    service: OnboardingService = Depends(get_onboarding_service),
) -> OnboardingSummaryOut:
    """
    Returns all onboarding data grouped by section — used to populate the
    Review step in the UI before the user finalises.

    `battery` and `grid` fields are `null` when not applicable for the
    chosen `system_type`.
    """
    return await service.get_summary(current_user.id)
