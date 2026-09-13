"""
SQLAlchemy ORM models for onboarding.

Tables
──────
solar_systems     — one per home; a user may own many; one is_primary
battery_configs   — one per system; only created for Off-grid / Hybrid
grid_configs      — one per system; only created for On-grid / Hybrid
tracked_appliances— many per system; the selected appliance catalog entries

Enum columns are stored as VARCHAR so migrations are cheap and the DB never
owns the enum type (works identically with Postgres and the SQLite test DB).
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.types import GUID
from app.modules.onboarding.household import generate_household_id
from app.modules.onboarding.enums import (
    ApplianceKey,
    BackupHours,
    InverterBrand,
    MeterType,
    OnboardingStep,
    PanelType,
    PrimaryGoal,
    SystemType,
    TariffType,
)

if TYPE_CHECKING:
    from app.models.user import User


class SolarSystem(Base):
    """
    Central record for a user's solar installation.
    Created in Step 1 (system + inverter); updated on subsequent steps.
    A user may own many homes; exactly one should be is_primary.
    """
    __tablename__ = "solar_systems"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Telemetry / simulator key. Unique across users; assigned once, never rotated.
    household_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True, default=generate_household_id
    )
    # One primary home per user. /energy/me/* and token-only dashboard URLs use this.
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Step 1: Solar panel info ─────────────────────────────────────────────
    panel_type: Mapped[str] = mapped_column(String(20), nullable=False)
    panel_qty:  Mapped[int] = mapped_column(Integer,     nullable=False)
    system_type: Mapped[str] = mapped_column(String(10), nullable=False)
    location:   Mapped[str | None] = mapped_column(String(200), nullable=True)
    avg_monthly_bill: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)

    # ── Step 2: Inverter info (combined in same DB record / same API call) ───
    inverter_brand:        Mapped[str]     = mapped_column(String(30),    nullable=False)
    inverter_capacity_kw:  Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    # Assistant / automation objective. VARCHAR like other enums (Python-owned).
    primary_goal: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=PrimaryGoal.MAXIMIZE_SELF_CONSUMPTION.value,
    )

    # ── Onboarding state ─────────────────────────────────────────────────────
    # last_step: furthest step the user completed (for resume on page reload)
    last_step:              Mapped[str]            = mapped_column(String(20), nullable=False, default=OnboardingStep.SYSTEM.value)
    appliances_configured:  Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    is_complete:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    completed_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    battery_config: Mapped["BatteryConfig | None"]    = relationship(back_populates="system", uselist=False, cascade="all, delete-orphan")
    grid_config:    Mapped["GridConfig | None"]        = relationship(back_populates="system", uselist=False, cascade="all, delete-orphan")
    appliances:     Mapped[list["TrackedAppliance"]] = relationship(back_populates="system", cascade="all, delete-orphan")


class BatteryConfig(Base):
    """
    Battery configuration — only present for Off-grid and Hybrid systems.
    Attempting to create this for On-grid raises SystemTypeConflictError (service layer).
    """
    __tablename__ = "battery_configs"

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    system_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("solar_systems.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    battery_capacity_kwh: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    backup_hours:         Mapped[int]     = mapped_column(Integer,        nullable=False)  # 2 | 4 | 8
    reserve_pct:          Mapped[int]     = mapped_column(Integer,        nullable=False)  # 5–50 %

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    system: Mapped["SolarSystem"] = relationship(back_populates="battery_config")


class GridConfig(Base):
    """
    Grid / tariff configuration — only present for On-grid and Hybrid systems.
    Attempting to create this for Off-grid raises SystemTypeConflictError (service layer).
    """
    __tablename__ = "grid_configs"

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    system_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("solar_systems.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    meter_type:          Mapped[str]          = mapped_column(String(20),    nullable=False)
    sanctioned_load_kw:  Mapped[Decimal]      = mapped_column(Numeric(6, 2), nullable=False)
    tariff_type:         Mapped[str]          = mapped_column(String(20),    nullable=False)
    discom:              Mapped[str | None]   = mapped_column(String(100),   nullable=True)
    energy_charge_inr_per_kwh: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("8.50")
    )
    export_credit_inr_per_kwh: Mapped[Decimal] = mapped_column(
        Numeric(8, 2), nullable=False, default=Decimal("6.20")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    system: Mapped["SolarSystem"] = relationship(back_populates="grid_config")


class TrackedAppliance(Base):
    """
    One row per selected appliance per system.
    `is_critical` = never auto-off (the "🔒 never auto-off" toggle in the UI).
    Replaced wholesale when the user re-submits the appliances step.
    """
    __tablename__ = "tracked_appliances"

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    system_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("solar_systems.id", ondelete="CASCADE"), nullable=False
    )
    appliance_key: Mapped[str]  = mapped_column(String(20), nullable=False)
    is_critical:   Mapped[bool] = mapped_column(Boolean,    nullable=False, default=False)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    system: Mapped["SolarSystem"] = relationship(back_populates="appliances")

    __table_args__ = (
        UniqueConstraint("system_id", "appliance_key", name="uq_system_appliance"),
    )
