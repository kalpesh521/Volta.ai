"""Recommendations, in-app alerts, and user automation rules."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import EnergyNotFoundError
from app.modules.assistant.actions.models import AutomationRule, HomeNotification
from app.modules.assistant.actions.service import ActionService
from app.modules.assistant.domain.alerts import alerts_from_home
from app.modules.assistant.notifications.cache import notification_cache
from app.modules.assistant.notifications.deliver import save_notification
from app.modules.assistant.notifications.serialize import notification_out
from app.modules.assistant.domain.recommendations import (
    DeviceOption,
    HourPoint,
    detect_abnormal_consumption,
    detect_high_grid_import,
    estimate_backup_hours,
    find_best_device_schedule,
    identify_solar_surplus_window,
)
from app.modules.assistant.ops.schemas import (
    AutomationIn,
    AutomationOut,
    AutomationResult,
    NotificationOut,
    RecommendationOut,
)
from app.modules.energy.service import EnergyService
from app.modules.onboarding.models import SolarSystem


class HomeOpsService:
    def __init__(self, db: AsyncSession, energy: EnergyService, actions: ActionService) -> None:
        self._db = db
        self._energy = energy
        self._actions = actions

    async def recommendations(self, system: SolarSystem) -> RecommendationOut:
        live = await self._live(system)
        if live is None:
            return RecommendationOut()
        hourly = await self._hours(system.household_id)
        daily_savings = await self._savings(system.household_id)
        surplus = round(live.solar.power_kw - live.load.power_kw, 3)
        reserve = _reserve(system)
        usable = None
        battery = system.__dict__.get("battery_config")
        if battery is not None and live.battery is not None:
            usable = float(battery.battery_capacity_kwh) * max(0.0, live.battery.soc_percent - reserve) / 100.0
        devices = [
            DeviceOption(
                device=item.device_type,
                rated_power_kw=float(item.rated_power_kw),
                controllable=bool(item.controllable),
                critical=bool(item.critical),
                state=item.current_state,
            )
            for item in live.devices
        ]
        soc = float(live.battery.soc_percent) if live.battery is not None else None
        return RecommendationOut(
            surplus_kw=surplus,
            backup_hours=estimate_backup_hours(usable_kwh=usable, load_kw=live.load.power_kw),
            estimated_savings_inr=daily_savings,
            surplus_window=identify_solar_surplus_window(hourly),
            high_grid_import=detect_high_grid_import(live.grid.import_power_kw),
            abnormal_consumption=detect_abnormal_consumption([point.home_kwh for point in hourly]),
            best_device=find_best_device_schedule(
                devices, surplus_kw=surplus, soc_percent=soc, reserve_percent=reserve
            ),
        )

    async def scan_alerts(self, system: SolarSystem, user_id: uuid.UUID) -> list[NotificationOut]:
        live = await self._live(system)
        if live is None:
            return []
        weather = live.weather
        advice = await self.recommendations(system)
        previous_outage = await self._last_grid_kind(system.household_id) == "grid_outage"
        drafts = alerts_from_home(
            soc_percent=float(live.battery.soc_percent) if live.battery is not None else None,
            reserve_percent=_reserve(system),
            grid_status=live.grid.status,
            import_kw=live.grid.import_power_kw,
            solar_kw=live.solar.power_kw,
            radiation_wm2=getattr(weather, "shortwave_radiation_wm2", None) if weather is not None else None,
            data_quality=live.data_quality,
            fault_code=getattr(live.battery, "fault_code", None) if live.battery is not None else None,
            previous_outage=previous_outage,
            daily_ready=advice.estimated_savings_inr is not None,
            insight=_insight_text(advice),
        )
        created: list[HomeNotification] = []
        for draft in drafts:
            row = await save_notification(
                self._db,
                user_id=user_id,
                household_id=system.household_id,
                kind=draft.kind,
                title=draft.title,
                body=draft.body,
                dedupe="today" if draft.kind == "daily_energy_summary_ready" else "unread",
            )
            if row is not None:
                created.append(row)
        return [notification_out(row) for row in created]

    async def list_notifications(self, system: SolarSystem, user_id: uuid.UUID) -> list[NotificationOut]:
        cached = await notification_cache.get_list(str(user_id), system.household_id)
        if cached is not None:
            return [NotificationOut.model_validate(item) for item in cached]
        result = await self._db.execute(
            select(HomeNotification)
            .where(HomeNotification.user_id == user_id, HomeNotification.household_id == system.household_id)
            .order_by(HomeNotification.created_at.desc())
            .limit(50)
        )
        rows = [notification_out(row) for row in result.scalars()]
        await notification_cache.set_list(
            str(user_id),
            system.household_id,
            [row.model_dump(mode="json") for row in rows],
        )
        return rows

    async def mark_read(self, system: SolarSystem, user_id: uuid.UUID, notification_id: uuid.UUID) -> NotificationOut | None:
        result = await self._db.execute(
            select(HomeNotification).where(
                HomeNotification.id == notification_id,
                HomeNotification.user_id == user_id,
                HomeNotification.household_id == system.household_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.read_at = datetime.now(timezone.utc)
        await self._db.flush()
        await notification_cache.invalidate(str(user_id), system.household_id)
        return notification_out(row)

    async def create_rule(self, system: SolarSystem, user_id: uuid.UUID, payload: AutomationIn) -> AutomationOut:
        row = AutomationRule(
            user_id=user_id,
            household_id=system.household_id,
            name=payload.name.strip(),
            device=payload.device,
            command=payload.command,
            surplus_kw_min=payload.surplus_kw_min,
            soc_percent_min=payload.soc_percent_min,
            mode=payload.mode,
            enabled=payload.mode != "disabled",
        )
        self._db.add(row)
        await self._db.flush()
        return _rule_out(row)

    async def list_rules(self, system: SolarSystem, user_id: uuid.UUID) -> list[AutomationOut]:
        result = await self._db.execute(
            select(AutomationRule)
            .where(AutomationRule.user_id == user_id, AutomationRule.household_id == system.household_id)
            .order_by(AutomationRule.created_at.desc())
        )
        return [_rule_out(row) for row in result.scalars()]

    async def evaluate(self, system: SolarSystem, user_id: uuid.UUID) -> list[AutomationResult]:
        live = await self._live(system)
        surplus = None if live is None else round(live.solar.power_kw - live.load.power_kw, 3)
        soc = None if live is None or live.battery is None else float(live.battery.soc_percent)
        rules = await self._db.execute(
            select(AutomationRule).where(
                AutomationRule.user_id == user_id,
                AutomationRule.household_id == system.household_id,
            )
        )
        results: list[AutomationResult] = []
        for rule in rules.scalars():
            matched = _matches(rule, surplus, soc)
            if not matched:
                results.append(AutomationResult(rule_id=rule.id, matched=False, mode=rule.mode))
                continue
            mode = rule.mode
            if mode == "automatic_for_approved_devices" and (
                not settings.DEVICE_CONTROL_ENABLED or settings.DEVICE_KILL_SWITCH
            ):
                mode = "require_confirmation"
            if mode == "suggest_only":
                note = await self._suggest(system, user_id, rule, surplus)
                results.append(
                    AutomationResult(rule_id=rule.id, matched=True, mode=mode, notification_id=note.id if note else None)
                )
                continue
            if mode in {"require_confirmation", "automatic_for_approved_devices"}:
                question = f"Turn {rule.command} the {rule.device.replace('_', ' ')}"
                proposal = await self._actions.propose(system, user_id, question)
                if mode == "automatic_for_approved_devices" and proposal.status == "awaiting_confirmation":
                    proposal = await self._actions.confirm(system, user_id, proposal.proposal_id, True)
                results.append(
                    AutomationResult(rule_id=rule.id, matched=True, mode=mode, proposal_id=proposal.proposal_id)
                )
                continue
            results.append(AutomationResult(rule_id=rule.id, matched=True, mode=mode))
        return results

    async def _suggest(self, system, user_id, rule: AutomationRule, surplus: float | None) -> HomeNotification | None:
        amount = f"{surplus:.1f} kW" if surplus is not None else "available solar"
        return await save_notification(
            self._db,
            user_id=user_id,
            household_id=system.household_id,
            kind="automation_suggestion",
            title=rule.name,
            body=f"Solar surplus is {amount}. {rule.command} the {rule.device.replace('_', ' ')}?",
            ref_id=str(rule.id),
        )

    async def _last_grid_kind(self, household_id: str) -> str | None:
        result = await self._db.execute(
            select(HomeNotification.kind).where(
                HomeNotification.household_id == household_id,
                HomeNotification.kind.in_(("grid_outage", "grid_restored")),
            ).order_by(HomeNotification.created_at.desc()).limit(1)
        )
        row = result.first()
        return None if row is None else row[0]

    async def _live(self, system: SolarSystem):
        try:
            return await self._energy.get_live(system.household_id, system.location)
        except EnergyNotFoundError:
            return None

    async def _hours(self, household_id: str) -> list[HourPoint]:
        try:
            summary = await self._energy.get_hourly(household_id)
        except EnergyNotFoundError:
            return []
        return [
            HourPoint(
                hour=bucket.hour_start.strftime("%H:%M"),
                solar_kwh=bucket.solar_generation_kwh,
                home_kwh=bucket.home_consumption_kwh,
                import_kwh=bucket.grid_import_kwh,
            )
            for bucket in summary.buckets
        ]

    async def _savings(self, household_id: str) -> float | None:
        try:
            daily = await self._energy.get_daily(household_id)
        except EnergyNotFoundError:
            return None
        amount = getattr(daily, "estimated_savings", None)
        return None if amount is None else float(amount)


def _reserve(system: SolarSystem) -> float:
    battery = system.__dict__.get("battery_config")
    if battery is None:
        return 20.0
    return float(battery.reserve_pct)


def _matches(rule: AutomationRule, surplus: float | None, soc: float | None) -> bool:
    if not rule.enabled or rule.mode == "disabled":
        return False
    if rule.surplus_kw_min is not None and (surplus is None or surplus <= rule.surplus_kw_min):
        return False
    if rule.soc_percent_min is not None and (soc is None or soc <= rule.soc_percent_min):
        return False
    return True


def _insight_text(advice: RecommendationOut) -> str | None:
    if advice.best_device:
        device = str(advice.best_device.get("device", "a device")).replace("_", " ")
        reason = advice.best_device.get("reason") or "Solar surplus can cover this load."
        return f"Run the {device} now. {reason}"
    if advice.abnormal_consumption:
        return "Home use is unusually high compared with recent hours."
    window = advice.surplus_window or {}
    if window.get("start_hour") and window.get("end_hour"):
        return f"Solar surplus window is {window['start_hour']} to {window['end_hour']}."
    return None


def _notification_out(row: HomeNotification) -> NotificationOut:
    return notification_out(row)


def _rule_out(row: AutomationRule) -> AutomationOut:
    return AutomationOut(
        id=row.id,
        name=row.name,
        device=row.device,
        command=row.command,
        surplus_kw_min=row.surplus_kw_min,
        soc_percent_min=row.soc_percent_min,
        mode=row.mode,  # type: ignore[arg-type]
        enabled=row.enabled,
    )
