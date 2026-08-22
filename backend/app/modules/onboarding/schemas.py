"""
Pydantic request/response schemas for the onboarding module.

Conditional field rules (enforced in the service layer, documented here):
  • BatteryStepIn  → valid only when system_type in {Off-grid, Hybrid}
  • GridStepIn     → valid only when system_type in {On-grid, Hybrid}
  • SystemStepIn combines UI step-1 (panels) + step-2 (inverter) into one payload
    because both fields live in the same DB row (solar_systems).
"""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.modules.onboarding.enums import (
    ApplianceKey,
    BackupHours,
    InverterBrand,
    MeterType,
    OnboardingStep,
    PanelType,
    SystemType,
    TariffType,
)


# ── Step 1+2: System + Inverter ───────────────────────────────────────────────

class SystemStepIn(BaseModel):
    """Covers UI step-1 (solar panel info) and step-2 (inverter) in one call."""
    # Step 1
    panel_type:        PanelType
    panel_qty:         int           = Field(ge=1, description="Number of panels (≥ 1)")
    system_type:       SystemType
    location:          str | None    = Field(default=None, max_length=200)
    avg_monthly_bill:  Decimal | None = Field(default=None, ge=0, description="Average monthly electricity bill in INR")

    # Step 2
    inverter_brand:       InverterBrand
    inverter_capacity_kw: Decimal     = Field(ge=1, description="Installed system capacity in kW")

    @field_validator("panel_qty")
    @classmethod
    def panel_qty_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("panel_qty must be at least 1")
        return v


# ── Step 3: Battery (Off-grid / Hybrid only) ──────────────────────────────────

class BatteryStepIn(BaseModel):
    """
    Only accepted when the system's system_type is Off-grid or Hybrid.
    Sending this for an On-grid system returns 422 (SystemTypeConflictError).
    """
    battery_capacity_kwh: Decimal = Field(ge=1,  description="Battery bank size in kWh")
    backup_hours:         BackupHours             # 2 | 4 | 8
    reserve_pct:          int     = Field(ge=5, le=50, description="Minimum SOC — never discharge below this %")


# ── Step 4: Grid (On-grid / Hybrid only) ─────────────────────────────────────

class GridStepIn(BaseModel):
    """
    Only accepted when the system's system_type is On-grid or Hybrid.
    Sending this for an Off-grid system returns 422 (SystemTypeConflictError).
    """
    meter_type:         MeterType
    sanctioned_load_kw: Decimal  = Field(ge=1, description="Contracted grid capacity in kW")
    tariff_type:        TariffType
    discom:             str | None = Field(default=None, max_length=100, description="Utility / DISCOM name (optional)")


# ── Step 5: Appliances ────────────────────────────────────────────────────────

class ApplianceIn(BaseModel):
    """Single appliance selection entry."""
    appliance_key: ApplianceKey
    is_critical:   bool = False  # True = never auto-off


class AppliancesStepIn(BaseModel):
    """
    The full appliance list is replaced wholesale on each PUT.
    An empty list is valid — it means the user deselected everything.
    """
    appliances: list[ApplianceIn] = Field(default_factory=list)

    @field_validator("appliances")
    @classmethod
    def no_duplicates(cls, items: list[ApplianceIn]) -> list[ApplianceIn]:
        seen = set()
        for item in items:
            if item.appliance_key in seen:
                raise ValueError(f"Duplicate appliance_key: {item.appliance_key}")
            seen.add(item.appliance_key)
        return items


# ── Response schemas ──────────────────────────────────────────────────────────

class BatteryConfigOut(BaseModel):
    battery_capacity_kwh: Decimal
    backup_hours:         int
    reserve_pct:          int

    model_config = {"from_attributes": True}


class GridConfigOut(BaseModel):
    meter_type:         str
    sanctioned_load_kw: Decimal
    tariff_type:        str
    discom:             str | None

    model_config = {"from_attributes": True}


class ApplianceOut(BaseModel):
    appliance_key: str
    is_critical:   bool

    model_config = {"from_attributes": True}


class SolarSystemOut(BaseModel):
    id:                   uuid.UUID
    panel_type:           str
    panel_qty:            int
    system_type:          str
    location:             str | None
    avg_monthly_bill:     Decimal | None
    inverter_brand:       str
    inverter_capacity_kw: Decimal
    last_step:            str
    is_complete:          bool
    completed_at:         datetime | None
    created_at:           datetime
    updated_at:           datetime

    model_config = {"from_attributes": True}


class OnboardingStatusOut(BaseModel):
    """
    Returned by GET /onboarding/status.
    Tells the frontend which step to resume from and what's still pending.
    """
    is_complete:       bool
    last_step:         str
    system_type:       str | None    # null if system step not done yet
    steps_completed:   list[str]
    steps_required:    list[str]     # all steps the user must complete (system-type aware)
    steps_remaining:   list[str]     # steps_required minus steps_completed


class OnboardingSummaryOut(BaseModel):
    """
    Returned by GET /onboarding/summary — used by the Review step in the UI.
    battery and grid are null when they don't apply to this system type.
    """
    system:     SolarSystemOut
    battery:    BatteryConfigOut | None
    grid:       GridConfigOut | None
    appliances: list[ApplianceOut]
    is_complete: bool
    last_step:   str
