"""
Data-access layer for onboarding.
No business rules here — only reads and writes. Repositories flush() so the
calling service (or FastAPI's get_db dependency) commits the transaction.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.onboarding.enums import OnboardingStep
from app.modules.onboarding.household import generate_household_id
from app.modules.onboarding.models import (
    BatteryConfig,
    GridConfig,
    SolarSystem,
    TrackedAppliance,
)


class OnboardingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── SolarSystem ───────────────────────────────────────────────────────────

    _SYSTEM_LOAD = (
        selectinload(SolarSystem.battery_config),
        selectinload(SolarSystem.grid_config),
        selectinload(SolarSystem.appliances),
    )

    async def list_systems_by_user(self, user_id: uuid.UUID) -> list[SolarSystem]:
        result = await self.db.execute(
            select(SolarSystem)
            .where(SolarSystem.user_id == user_id)
            .options(*self._SYSTEM_LOAD)
            .order_by(SolarSystem.is_primary.desc(), SolarSystem.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def count_systems(self, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.count()).select_from(SolarSystem).where(SolarSystem.user_id == user_id)
        )
        return int(result.scalar_one())

    async def get_primary_system(self, user_id: uuid.UUID) -> SolarSystem | None:
        result = await self.db.execute(
            select(SolarSystem)
            .where(SolarSystem.user_id == user_id, SolarSystem.is_primary.is_(True))
            .options(*self._SYSTEM_LOAD)
        )
        rows = list(result.scalars().unique().all())
        if rows:
            return rows[0]
        homes = await self.list_systems_by_user(user_id)
        return homes[0] if homes else None

    async def get_system_by_user(self, user_id: uuid.UUID) -> SolarSystem | None:
        """Backward-compatible alias: the user's primary home."""
        return await self.get_primary_system(user_id)

    async def get_completed_systems(self) -> list[SolarSystem]:
        """All finished homes — used by the simulator to generate ticks."""
        result = await self.db.execute(
            select(SolarSystem)
            .where(SolarSystem.is_complete.is_(True))
            .options(*self._SYSTEM_LOAD)
            .order_by(SolarSystem.created_at.asc())
        )
        return list(result.scalars().unique().all())

    async def set_primary(self, user_id: uuid.UUID, system: SolarSystem) -> SolarSystem:
        """Clear every home, then mark this one. Avoids a unique-index clash."""
        now = datetime.now(timezone.utc)
        await self.db.execute(
            update(SolarSystem)
            .where(SolarSystem.user_id == user_id)
            .values(is_primary=False, updated_at=now)
        )
        await self.db.flush()
        return await self.update_system(system, is_primary=True)

    async def get_system_by_household(self, household_id: str) -> SolarSystem | None:
        """Look up by telemetry household_id. Used for energy authorization."""
        result = await self.db.execute(
            select(SolarSystem)
            .where(SolarSystem.household_id == household_id)
            .options(*self._SYSTEM_LOAD)
        )
        return result.scalar_one_or_none()

    async def create_system(
        self,
        *,
        user_id: uuid.UUID,
        panel_type: str,
        panel_qty: int,
        system_type: str,
        location: str | None,
        avg_monthly_bill: Decimal | None,
        inverter_brand: str,
        inverter_capacity_kw: Decimal,
        is_primary: bool = False,
        primary_goal: str = "maximize_self_consumption",
    ) -> SolarSystem:
        system = SolarSystem(
            user_id=user_id,
            household_id=generate_household_id(),
            is_primary=is_primary,
            panel_type=panel_type,
            panel_qty=panel_qty,
            system_type=system_type,
            location=location,
            avg_monthly_bill=avg_monthly_bill,
            inverter_brand=inverter_brand,
            inverter_capacity_kw=inverter_capacity_kw,
            primary_goal=primary_goal,
            last_step=OnboardingStep.SYSTEM.value,
        )
        self.db.add(system)
        await self.db.flush()
        # Reload server defaults (created_at, updated_at) for Pydantic serialization.
        await self.db.refresh(system)
        return system

    async def update_system(self, system: SolarSystem, **fields) -> SolarSystem:
        """Apply arbitrary column updates and flush (no full replace)."""
        for key, value in fields.items():
            setattr(system, key, value)
        # Set in Python — server-side onupdate does not populate the ORM object
        # after flush, which causes MissingGreenlet when Pydantic reads updated_at.
        system.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(system)
        return system

    # ── BatteryConfig ─────────────────────────────────────────────────────────

    async def upsert_battery(
        self,
        *,
        system: SolarSystem,
        battery_capacity_kwh: Decimal,
        backup_hours: int,
        reserve_pct: int,
    ) -> BatteryConfig:
        """Create or fully replace the battery record for this system."""
        if system.battery_config:
            bc = system.battery_config
            bc.battery_capacity_kwh = battery_capacity_kwh
            bc.backup_hours         = backup_hours
            bc.reserve_pct          = reserve_pct
        else:
            bc = BatteryConfig(
                system_id=system.id,
                battery_capacity_kwh=battery_capacity_kwh,
                backup_hours=backup_hours,
                reserve_pct=reserve_pct,
            )
            self.db.add(bc)
            system.battery_config = bc
        await self.db.flush()
        return bc

    async def delete_battery(self, system: SolarSystem) -> None:
        """Remove battery config when system_type changes to On-grid."""
        if system.battery_config:
            await self.db.delete(system.battery_config)
            system.battery_config = None
            await self.db.flush()

    # ── GridConfig ────────────────────────────────────────────────────────────

    async def upsert_grid(
        self,
        *,
        system: SolarSystem,
        meter_type: str,
        sanctioned_load_kw: Decimal,
        tariff_type: str,
        discom: str | None,
        energy_charge_inr_per_kwh: Decimal,
        export_credit_inr_per_kwh: Decimal,
    ) -> GridConfig:
        """Create or fully replace the grid record for this system."""
        if system.grid_config:
            gc = system.grid_config
            gc.meter_type          = meter_type
            gc.sanctioned_load_kw  = sanctioned_load_kw
            gc.tariff_type         = tariff_type
            gc.discom              = discom
            gc.energy_charge_inr_per_kwh = energy_charge_inr_per_kwh
            gc.export_credit_inr_per_kwh = export_credit_inr_per_kwh
        else:
            gc = GridConfig(
                system_id=system.id,
                meter_type=meter_type,
                sanctioned_load_kw=sanctioned_load_kw,
                tariff_type=tariff_type,
                discom=discom,
                energy_charge_inr_per_kwh=energy_charge_inr_per_kwh,
                export_credit_inr_per_kwh=export_credit_inr_per_kwh,
            )
            self.db.add(gc)
            system.grid_config = gc
        await self.db.flush()
        return gc

    async def delete_grid(self, system: SolarSystem) -> None:
        """Remove grid config when system_type changes to Off-grid."""
        if system.grid_config:
            await self.db.delete(system.grid_config)
            system.grid_config = None
            await self.db.flush()

    # ── TrackedAppliances ─────────────────────────────────────────────────────

    async def replace_appliances(
        self,
        *,
        system: SolarSystem,
        appliances: list[dict],  # [{"appliance_key": str, "is_critical": bool}]
    ) -> list[TrackedAppliance]:
        """
        Delete all existing appliance rows then bulk-insert the new selection.
        Wholesale replace is simpler than diffing and avoids stale rows.
        """
        await self.db.execute(
            delete(TrackedAppliance).where(TrackedAppliance.system_id == system.id)
        )
        rows = [
            TrackedAppliance(
                system_id=system.id,
                appliance_key=item["appliance_key"],
                is_critical=item["is_critical"],
            )
            for item in appliances
        ]
        self.db.add_all(rows)
        await self.db.flush()
        system.appliances = rows
        return rows
