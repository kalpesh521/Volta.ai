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


class OnboardingService:
    def __init__(self, repo: OnboardingRepository) -> None:
        self.repo = repo

    # ── Step 1+2: System + Inverter ───────────────────────────────────────────

    async def save_system(self, user_id: uuid.UUID, data: SystemStepIn) -> SolarSystem:
        """
        Upsert the SolarSystem record.
        If system_type changes, stale battery/grid records are cleaned up.
        """
        system = await self.repo.get_system_by_user(user_id)

        if system is None:
            # First time — create the record
            system = await self.repo.create_system(
                user_id=user_id,
                panel_type=data.panel_type.value,
                panel_qty=data.panel_qty,
                system_type=data.system_type.value,
                location=data.location,
                avg_monthly_bill=data.avg_monthly_bill,
                inverter_brand=data.inverter_brand.value,
                inverter_capacity_kw=data.inverter_capacity_kw,
            )
        else:
            # Subsequent edit — handle system_type transitions
            old_type = system.system_type
            new_type = data.system_type.value

            if old_type != new_type:
                # On-grid no longer needs battery
                if new_type == SystemType.ON_GRID.value:
                    await self.repo.delete_battery(system)
                # Off-grid no longer needs grid
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
                last_step=OnboardingStep.SYSTEM.value,
            )

        return system

    # ── Step 3: Battery ───────────────────────────────────────────────────────

    async def save_battery(self, user_id: uuid.UUID, data: BatteryStepIn) -> SolarSystem:
        """Stores battery config. Raises 422 for On-grid systems."""
        system = await self._require_system(user_id)

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

    async def save_grid(self, user_id: uuid.UUID, data: GridStepIn) -> SolarSystem:
        """Stores grid / tariff config. Raises 422 for Off-grid systems."""
        system = await self._require_system(user_id)

        if system.system_type not in _NEEDS_GRID:
            raise SystemTypeConflictError(
                f"Grid configuration is not applicable for '{system.system_type}' systems. "
                "Only On-grid and Hybrid systems connect to the grid."
            )

        await self.repo.upsert_grid(
            system=system,
            meter_type=data.meter_type.value,
            sanctioned_load_kw=data.sanctioned_load_kw,
            tariff_type=data.tariff_type.value,
            discom=data.discom,
        )
        await self.repo.update_system(system, last_step=OnboardingStep.GRID.value)
        return system

    # ── Step 5: Appliances ────────────────────────────────────────────────────

    async def save_appliances(self, user_id: uuid.UUID, data: AppliancesStepIn) -> SolarSystem:
        """Replace the appliance list wholesale. Empty list is valid."""
        system = await self._require_system(user_id)

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

    async def complete_onboarding(self, user_id: uuid.UUID) -> SolarSystem:
        """
        Mark onboarding as done after validating all required steps are complete.
        Required steps per system_type:
            On-grid  → system + grid    + appliances
            Off-grid → system + battery + appliances
            Hybrid   → system + battery + grid + appliances
        """
        system = await self._require_system(user_id)

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

    async def get_status(self, user_id: uuid.UUID) -> OnboardingStatusOut:
        system = await self.repo.get_system_by_user(user_id)

        if system is None:
            return OnboardingStatusOut(
                is_complete=False,
                last_step=OnboardingStep.SYSTEM.value,
                system_type=None,
                steps_completed=[],
                steps_required=["system"],
                steps_remaining=["system"],
            )

        completed  = self._completed_steps(system)
        required   = self._required_steps(system.system_type)
        remaining  = [s for s in required if s not in completed]

        return OnboardingStatusOut(
            is_complete=system.is_complete,
            last_step=system.last_step,
            system_type=system.system_type,
            steps_completed=completed,
            steps_required=required,
            steps_remaining=remaining,
        )

    async def get_summary(self, user_id: uuid.UUID) -> OnboardingSummaryOut:
        """Full onboarding data — used by the Review screen."""
        system = await self._require_system(user_id)

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

    async def _require_system(self, user_id: uuid.UUID) -> SolarSystem:
        """Load system or raise 404 — used by steps that need system to exist first."""
        system = await self.repo.get_system_by_user(user_id)
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
