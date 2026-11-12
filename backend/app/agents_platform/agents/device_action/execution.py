"""Persist a virtual-device result after the command has been queued."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.agents.device_action.models import ActionAuditEvent, ActionProposal, DeviceOverride
from app.agents_platform.agents.device_action.worker import handle_device_command
from app.modules.assistant.notifications.deliver import save_notification


async def record_virtual_result(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    proposal = await _proposal_for_command(db, str(payload.get("command_id") or ""))
    if proposal is not None and proposal.status == "cancelled":
        return {"applied": False, "status": "cancelled", "command_id": payload.get("command_id")}
    result = handle_device_command(payload)
    now = datetime.now(timezone.utc)
    if proposal is not None:
        proposal.status = "applied" if result.get("applied") else "failed"
        proposal.updated_at = now
        _audit(
            db,
            proposal,
            event="applied" if result.get("applied") else "failed",
            command_id=result.get("command_id"),
        )
        if not result.get("applied"):
            await _notify(
                db,
                user_id=proposal.user_id,
                household_id=proposal.household_id,
                kind="device_command_failed",
                title="Device command failed",
                body="The virtual device did not accept the command.",
                ref_id=proposal.id,
            )
    if result.get("applied") and proposal is not None:
        await _activate_override(db, proposal, result, now)
    await db.flush()
    return result


async def _proposal_for_command(db: AsyncSession, command_id: str) -> ActionProposal | None:
    if not command_id:
        return None
    result = await db.execute(select(ActionProposal).where(ActionProposal.command_id == command_id))
    return result.scalar_one_or_none()


async def _activate_override(db: AsyncSession, proposal: ActionProposal, result: dict[str, Any], now: datetime) -> None:
    existing = await db.execute(
        select(DeviceOverride).where(
            DeviceOverride.household_id == proposal.household_id,
            DeviceOverride.device == result["device"],
            DeviceOverride.status == "active",
        )
    )
    for row in existing.scalars():
        row.status = "cleared"
    minutes = result.get("duration_minutes")
    expires = now + timedelta(minutes=int(minutes)) if minutes else None
    db.add(
        DeviceOverride(
            household_id=proposal.household_id,
            device=result["device"],
            command=result["command"],
            power_kw=result.get("power_kw"),
            duration_minutes=int(minutes) if minutes else None,
            command_id=result["command_id"],
            proposal_id=proposal.id,
            status="active",
            expires_at=expires,
        )
    )


def _audit(db: AsyncSession, proposal: ActionProposal, *, event: str, command_id: str | None) -> None:
    db.add(
        ActionAuditEvent(
            proposal_id=proposal.id,
            user_id=proposal.user_id,
            household_id=proposal.household_id,
            event=event,
            reasons_json=proposal.reasons_json,
            checks_json=proposal.checks_json,
            command_id=command_id,
        )
    )


async def _notify(
    db: AsyncSession,
    *,
    user_id,
    household_id: str,
    kind: str,
    title: str,
    body: str,
    ref_id: str | None,
) -> None:
    await save_notification(
        db,
        user_id=user_id,
        household_id=household_id,
        kind=kind,
        title=title,
        body=body,
        ref_id=ref_id,
    )


def command_payload(command) -> dict[str, Any]:
    return {
        "command_id": command.command_id,
        "household_id": command.household_id,
        "user_id": command.user_id,
        "device": command.device,
        "command": command.command,
        "duration_minutes": command.duration_minutes,
        "power_kw": command.power_kw,
    }
