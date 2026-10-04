"""
Request-scoped, read-only access to one household's energy data.

Battery, grid, devices, weather and live are projections of the same latest
tick, so the gateway fetches that tick once (single-flight: concurrent callers
await the same task) and serves every projection from it.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import TypeVar

from app.core.exceptions import EnergyNotFoundError
from app.modules.assistant.domain.context import OnboardingDetails
from app.modules.energy.profile import build_simulator_profile
from app.modules.energy.schemas import (
    DailySummaryOut,
    HourlySummaryOut,
    LiveEnergyOut,
    SimulatorProfileOut,
    TelemetryRecord,
)
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem

T = TypeVar("T")


class EnergyDataGateway:
    def __init__(self, service: EnergyService, system: SolarSystem) -> None:
        self._service = service
        self._system = system
        self._tasks: dict[str, asyncio.Task] = {}
        self._profile: SimulatorProfileOut | None = None

    @property
    def household_id(self) -> str:
        return self._system.household_id

    async def _once(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        task = self._tasks.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._tasks[key] = task
        return await asyncio.shield(task)

    async def _or_none(self, coro: Awaitable[T]) -> T | None:
        try:
            return await coro
        except EnergyNotFoundError:
            return None

    async def live(self) -> LiveEnergyOut | None:
        return await self._once(
            "live",
            lambda: self._or_none(
                self._service.get_live(self._system.household_id, self._system.location)
            ),
        )

    async def daily(self, day: date | None = None) -> DailySummaryOut | None:
        return await self._once(
            f"daily:{day}",
            lambda: self._or_none(
                self._service.get_daily(self._system.household_id, day=day, system=self._system)
            ),
        )

    async def hourly(self, day: date | None = None) -> HourlySummaryOut | None:
        return await self._once(
            f"hourly:{day}",
            lambda: self._or_none(self._service.get_hourly(self._system.household_id, day=day)),
        )

    async def history(self, limit: int) -> list[TelemetryRecord]:
        async def load() -> list[TelemetryRecord]:
            try:
                return (await self._service.get_history(self._system.household_id, limit=limit)).records
            except EnergyNotFoundError:
                return []

        return await self._once(f"history:{limit}", load)

    def profile(self) -> SimulatorProfileOut:
        if self._profile is None:
            self._profile = build_simulator_profile(self._system)
        return self._profile

    def onboarding_details(self) -> OnboardingDetails:
        """Onboarding answers not carried by the simulator profile. No identity fields."""
        system = self._system
        battery = system.battery_config
        grid = system.grid_config
        return OnboardingDetails(
            panel_type=system.panel_type,
            panel_qty=system.panel_qty,
            inverter_brand=system.inverter_brand,
            avg_monthly_bill_inr=float(system.avg_monthly_bill) if system.avg_monthly_bill is not None else None,
            battery_backup_hours_target=battery.backup_hours if battery else None,
            sanctioned_load_kw=float(grid.sanctioned_load_kw) if grid else None,
            discom=grid.discom if grid else None,
        )
