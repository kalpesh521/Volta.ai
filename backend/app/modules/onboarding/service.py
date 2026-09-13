"""
Onboarding business logic.

Key rules enforced here (not in the router):
  • battery step → system_type must be Off-grid or Hybrid
  • grid step    → system_type must be On-grid  or Hybrid
  • complete()   → all required steps must be done for the chosen system_type
  • If the user re-submits the system step and changes system_type:
      – On-grid  becomes Off-grid/Hybrid  → battery step is now required (existing grid may stay)
      – Off-grid/Hybrid becomes On-grid   → delete battery config (no longer applicable)
      – Hybrid  becomes Off-grid          → delete grid config
"""
import uuid
from datetime import datetime, timezone

from decimal import Decimal

from app.core.config import settings
from app.core.exceptions import (
    ForbiddenError,
    OnboardingAlreadyCompleteError,
    OnboardingNotFoundError,
    SystemTypeConflictError,
)
from app.modules.onboarding.enums import OnboardingStep, SystemType
from app.modules.onboarding.models import SolarSystem
from app.modules.onboarding.repository import OnboardingRepository
from app.modules.onboarding.schemas import (
    AppliancesStepIn,
    BatteryStepIn,
    GridStepIn,
    OnboardingStatusOut,
    OnboardingSummaryOut,
    SystemStepIn,
)

# System types that require battery / grid steps
_NEEDS_BATTERY = {SystemType.OFF_GRID.value, SystemType.HYBRID.value}
_NEEDS_GRID    = {SystemType.ON_GRID.value,  SystemType.HYBRID.value}


def _default_energy_charge() -> Decimal:
    return Decimal(str(settings.DEFAULT_ENERGY_CHARGE_INR_PER_KWH))


def _default_export_credit() -> Decimal:
    return Decimal(str(settings.DEFAULT_EXPORT_CREDIT_INR_PER_KWH))


def _resolve_rate(
    incoming: Decimal | None, existing: Decimal | None, default: Decimal
) -> Decimal:
    if incoming is not None:
        return incoming
    if existing is not None:
        return existing
    return default


class OnboardingService:
    def __init__(self, repo: OnboardingRepository) -> None:
        self.repo = repo

    # ── Step 1+2: System + Inverter ───────────────────────────────────────────

    async def save_system(
        self,
        user_id: uuid.UUID,
        data: SystemStepIn,
        household_id: str | None = None,
        *,
        create_new: bool = False,
    ) -> SolarSystem:
        """
        Create or update a home.
        create_new=True always inserts another home (multi-home).
        household_id targets a specific home; omit to use/create the primary.
        """
        if create_new:
            count = await self.repo.count_systems(user_id)
            return await self.repo.create_system(
                user_id=user_id,
                panel_type=data.panel_type.value,
                panel_qty=data.panel_qty,
                system_type=data.system_type.value,
                location=data.location,
                avg_monthly_bill=data.avg_monthly_bill,
                inverter_brand=data.inverter_brand.value,
                inverter_capacity_kw=data.inverter_capacity_kw,
                is_primary=(count == 0),
                primary_goal=data.primary_goal.value,
            )

        if household_id:
            system = await self._require_system(user_id, household_id)
            return await self._apply_system_update(system, data)

        existing = await self.repo.get_primary_system(user_id)
        if existing is None:
            return await self.repo.create_system(
                user_id=user_id,
                panel_type=data.panel_type.value,
                panel_qty=data.panel_qty,
                system_type=data.system_type.value,
                location=data.location,
                avg_monthly_bill=data.avg_monthly_bill,
                inverter_brand=data.inverter_brand.value,
                inverter_capacity_kw=data.inverter_capacity_kw,
                is_primary=True,
                primary_goal=data.primary_goal.value,
            )
        return await self._apply_system_update(existing, data)

    async def _apply_system_update(self, system: SolarSystem, data: SystemStepIn) -> SolarSystem:
        old_type = system.system_type
        new_type = data.system_type.value
        if old_type != new_type:
            if new_type == SystemType.ON_GRID.value:
                await self.repo.delete_battery(system)
            if new_type == SystemType.OFF_GRID.value:
                await self.repo.delete_grid(system)
        await self.repo.update_system(
            system,
            panel_type=data.panel_type.value,
            panel_qty=data.panel_qty,
            system_type=new_type,
            location=data.location,
            avg_monthly_bill=data.avg_monthly_bill,
            inverter_brand=data.inverter_brand.value,
            inverter_capacity_kw=data.inverter_capacity_kw,
            primary_goal=data.primary_goal.value,
            last_step=OnboardingStep.SYSTEM.value,
        )
        return system

    async def list_homes(self, user_id: uuid.UUID):
        from app.modules.onboarding.schemas import HomeListItem, HomeListOut

        homes = await self.repo.list_systems_by_user(user_id)
        primary = next((h.household_id for h in homes if h.is_primary), None)
        if primary is None and homes:
            primary = homes[0].household_id
        return HomeListOut(
            primary_household_id=primary,
            homes=[
                HomeListItem(
                    household_id=h.household_id,
                    is_primary=h.is_primary,
                    is_complete=h.is_complete,
                    system_type=h.system_type,
                    location=h.location,
                    last_step=h.last_step,
                )
                for h in homes
            ],
        )

    async def set_primary(self, user_id: uuid.UUID, household_id: str) -> SolarSystem:
        system = await self._require_system(user_id, household_id)
        return await self.repo.set_primary(user_id, system)

    # ── Step 3: Battery ───────────────────────────────────────────────────────

    async def save_battery(
        self, user_id: uuid.UUID, data: BatteryStepIn, household_id: str | None = None
    ) -> SolarSystem:
        """Stores battery config. Raises 422 for On-grid systems."""
        system = await self._require_system(user_id, household_id)

        if system.system_type not in _NEEDS_BATTERY:
            raise SystemTypeConflictError(
                f"Battery configuration is not applicable for '{system.system_type}' systems. "
                "Only Off-grid and Hybrid systems have a battery."
            )

        await self.repo.upsert_battery(
            system=system,
            battery_capacity_kwh=data.battery_capacity_kwh,
            backup_hours=int(data.backup_hours),
            reserve_pct=data.reserve_pct,
        )
        await self.repo.update_system(system, last_step=OnboardingStep.BATTERY.value)
        return system

    # ── Step 4: Grid ──────────────────────────────────────────────────────────

    async def save_grid(
        self, user_id: uuid.UUID, data: GridStepIn, household_id: str | None = None
    ) -> SolarSystem:
        """Stores grid / tariff config. Raises 422 for Off-grid systems."""
        system = await self._require_system(user_id, household_id)

        if system.system_type not in _NEEDS_GRID:
            raise SystemTypeConflictError(
                f"Grid configuration is not applicable for '{system.system_type}' systems. "
                "Only On-grid and Hybrid systems connect to the grid."
            )

        existing = system.grid_config
        await self.repo.upsert_grid(
            system=system,
            meter_type=data.meter_type.value,
            sanctioned_load_kw=data.sanctioned_load_kw,
            tariff_type=data.tariff_type.value,
            discom=data.discom,
            energy_charge_inr_per_kwh=_resolve_rate(
                data.energy_charge_inr_per_kwh,
                existing.energy_charge_inr_per_kwh if existing else None,
                _default_energy_charge(),
            ),
            export_credit_inr_per_kwh=_resolve_rate(
                data.export_credit_inr_per_kwh,
                existing.export_credit_inr_per_kwh if existing else None,
                _default_export_credit(),
            ),
        )
        await self.repo.update_system(system, last_step=OnboardingStep.GRID.value)
        return system

    # ── Step 5: Appliances ────────────────────────────────────────────────────

    async def save_appliances(
        self, user_id: uuid.UUID, data: AppliancesStepIn, household_id: str | None = None
    ) -> SolarSystem:
        """Replace the appliance list wholesale. Empty list is valid."""
        system = await self._require_system(user_id, household_id)

        await self.repo.replace_appliances(
            system=system,
            appliances=[
                {"appliance_key": a.appliance_key.value, "is_critical": a.is_critical}
                for a in data.appliances
            ],
        )
        await self.repo.update_system(
            system,
            last_step=OnboardingStep.APPLIANCES.value,
            appliances_configured=True,
        )
        return system

    # ── Complete ──────────────────────────────────────────────────────────────

    async def complete_onboarding(
        self, user_id: uuid.UUID, household_id: str | None = None
    ) -> SolarSystem:
        """
        Mark onboarding as done after validating all required steps are complete.
        """
        system = await self._require_system(user_id, household_id)

        if system.is_complete:
            raise OnboardingAlreadyCompleteError()

        missing = self._missing_steps(system)
        if missing:
            raise ForbiddenError(
                f"Cannot complete onboarding. Missing steps: {', '.join(missing)}"
            )

        await self.repo.update_system(
            system,
            is_complete=True,
            completed_at=datetime.now(timezone.utc),
            last_step=OnboardingStep.COMPLETE.value,
        )
        return system

    # ── Read ──────────────────────────────────────────────────────────────────

    async def get_status(
        self, user_id: uuid.UUID, household_id: str | None = None
    ) -> OnboardingStatusOut:
        if household_id:
            system = await self.repo.get_system_by_household(household_id)
            if system is None or system.user_id != user_id:
                system = None
        else:
            system = await self.repo.get_primary_system(user_id)

        if system is None:
            return OnboardingStatusOut(
                is_complete=False,
                last_step=OnboardingStep.SYSTEM.value,
                system_type=None,
                household_id=None,
                is_primary=False,
                steps_completed=[],
                steps_required=["system"],
                steps_remaining=["system"],
                primary_goal=None,
            )

        completed  = self._completed_steps(system)
        required   = self._required_steps(system.system_type)
        remaining  = [s for s in required if s not in completed]

        return OnboardingStatusOut(
            is_complete=system.is_complete,
            last_step=system.last_step,
            system_type=system.system_type,
            household_id=system.household_id,
            is_primary=system.is_primary,
            steps_completed=completed,
            steps_required=required,
            steps_remaining=remaining,
            primary_goal=system.primary_goal,
        )

    async def get_summary(
        self, user_id: uuid.UUID, household_id: str | None = None
    ) -> OnboardingSummaryOut:
        """Full onboarding data — used by the Review screen."""
        system = await self._require_system(user_id, household_id)

        from app.modules.onboarding.schemas import (
            ApplianceOut,
            BatteryConfigOut,
            GridConfigOut,
            SolarSystemOut,
        )

        return OnboardingSummaryOut(
            system=SolarSystemOut.model_validate(system),
            battery=(
                BatteryConfigOut.model_validate(system.battery_config)
                if system.battery_config else None
            ),
            grid=(
                GridConfigOut.model_validate(system.grid_config)
                if system.grid_config else None
            ),
            appliances=[
                ApplianceOut.model_validate(a) for a in system.appliances
            ],
            is_complete=system.is_complete,
            last_step=system.last_step,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _require_system(
        self, user_id: uuid.UUID, household_id: str | None = None
    ) -> SolarSystem:
        """Load a home the user owns, or the primary home if household_id is omitted."""
        if household_id:
            system = await self.repo.get_system_by_household(household_id)
            if system is None or system.user_id != user_id:
                raise OnboardingNotFoundError()
            return system
        system = await self.repo.get_primary_system(user_id)
        if system is None:
            raise OnboardingNotFoundError()
        return system

    def _required_steps(self, system_type: str) -> list[str]:
        steps = ["system"]
        if system_type in _NEEDS_BATTERY:
            steps.append("battery")
        if system_type in _NEEDS_GRID:
            steps.append("grid")
        steps.append("appliances")
        return steps

    def _completed_steps(self, system: SolarSystem) -> list[str]:
        done = ["system"]  # SolarSystem record exists → step done
        if system.battery_config is not None:
            done.append("battery")
        if system.grid_config is not None:
            done.append("grid")
        if system.appliances_configured:
            done.append("appliances")
        return done

    def _missing_steps(self, system: SolarSystem) -> list[str]:
        required  = set(self._required_steps(system.system_type))
        completed = set(self._completed_steps(system))
        return sorted(required - completed)
