"""
Housekeeping for device-action proposals. Run it at least once per retention period:

    python -m app.modules.assistant.actions.maintenance

1. Proposals nobody answered before `expires_at` are recorded as `expired`, with an audit event.
2. LangGraph checkpoints of proposals that finished (or expired) more than
   `ACTION_CHECKPOINT_RETENTION_HOURS` ago are deleted. The proposal row and its audit
   trail stay; only the paused graph state goes. The lookback window is one retention
   period wide, so a sweep that runs at least that often sees every proposal once.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.checkpointer import CheckpointerProvider
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.core.metrics import ACTION_PROPOSALS
from app.modules.assistant.actions.models import ActionAuditEvent, ActionProposal
from app.modules.assistant.actions.service import AWAITING, CONFIRMING

logger = logging.getLogger("volta.actions.maintenance")

_OPEN_STATUSES = (AWAITING, CONFIRMING)


@dataclass(frozen=True)
class SweepResult:
    expired: int
    pruned_threads: int


async def expire_waiting(db: AsyncSession, *, now: datetime) -> int:
    """Mark unanswered proposals expired. Returns how many changed."""
    rows = (
        await db.execute(
            select(ActionProposal).where(
                ActionProposal.status == AWAITING,
                ActionProposal.expires_at.is_not(None),
                ActionProposal.expires_at <= now,
            )
        )
    ).scalars().all()
    for proposal in rows:
        proposal.status = "expired"
        proposal.expires_at = None
        proposal.updated_at = now
        db.add(
            ActionAuditEvent(
                proposal_id=proposal.id,
                user_id=proposal.user_id,
                household_id=proposal.household_id,
                event="expired",
                reasons_json=proposal.reasons_json,
                checks_json=proposal.checks_json,
                command_id=proposal.command_id,
            )
        )
        ACTION_PROPOSALS.labels(status="expired").inc()
    await db.flush()
    return len(rows)


async def prune_checkpoints(
    db: AsyncSession, provider: CheckpointerProvider, *, now: datetime, retention_hours: int
) -> int:
    """Delete checkpoints of proposals that ended inside the lookback window. Returns threads processed."""
    cutoff = now - timedelta(hours=retention_hours)
    window_start = cutoff - timedelta(hours=retention_hours)
    thread_ids = list(
        (
            await db.execute(
                select(ActionProposal.id).where(
                    ActionProposal.status.not_in(_OPEN_STATUSES),
                    ActionProposal.updated_at <= cutoff,
                    ActionProposal.updated_at > window_start,
                )
            )
        ).scalars()
    )
    await provider.delete_threads(thread_ids)
    return len(thread_ids)


async def sweep(
    db: AsyncSession,
    provider: CheckpointerProvider,
    *,
    retention_hours: int | None = None,
    now: datetime | None = None,
) -> SweepResult:
    moment = now or datetime.now(timezone.utc)
    hours = retention_hours if retention_hours is not None else settings.ACTION_CHECKPOINT_RETENTION_HOURS
    expired = await expire_waiting(db, now=moment)
    pruned = await prune_checkpoints(db, provider, now=moment, retention_hours=hours)
    return SweepResult(expired=expired, pruned_threads=pruned)


async def _run() -> SweepResult:
    provider = CheckpointerProvider()
    await provider.start()
    try:
        async with AsyncSessionLocal() as session:
            result = await sweep(session, provider)
            await session.commit()
            return result
    finally:
        await provider.stop()


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    result = asyncio.run(_run())
    logger.info("action maintenance expired=%d pruned_threads=%d", result.expired, result.pruned_threads)


if __name__ == "__main__":
    main()
