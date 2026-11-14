"""Save a notification, invalidate the list cache, then fan out to online users and email."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.modules.assistant.notifications.models import HomeNotification
from app.modules.assistant.domain.alerts import priority_for
from app.modules.assistant.notifications.bus import publish
from app.modules.assistant.notifications.cache import notification_cache
from app.modules.assistant.notifications.email import email_configured
from app.modules.assistant.notifications.serialize import notification_event

logger = logging.getLogger("volta.notifications")


async def save_notification(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    household_id: str,
    kind: str,
    title: str,
    body: str,
    ref_id: str | None = None,
    dedupe: str = "unread",
) -> HomeNotification | None:
    if dedupe == "today" and await _created_today(db, household_id, kind):
        return None
    if dedupe not in ("today", "none") and await _open_kind(db, household_id, kind):
        return None  # "none" is for callers that already rate-limit themselves (a rule has a cooldown and a daily cap)
    priority = priority_for(kind)
    row = HomeNotification(
        user_id=user_id,
        household_id=household_id,
        kind=kind,
        priority=priority,
        title=title[:160],
        body=body[:500],
        ref_id=ref_id,
        email_status="pending" if priority == "critical" else "skipped",
    )
    db.add(row)
    await db.flush()
    await fan_out(db, row)
    return row


async def fan_out(db: AsyncSession, row: HomeNotification) -> None:
    await publish(row.household_id, notification_event(row))
    await notification_cache.invalidate(str(row.user_id), row.household_id)
    if row.priority != "critical":
        return
    email = await _user_email(db, row.user_id)
    row.email_status = enqueue_alert_email(email, row.title, row.body)


def enqueue_alert_email(to_email: str | None, subject: str, text: str) -> str:
    if not to_email or not email_configured():
        return "skipped"
    try:
        from app.modules.assistant.notifications.tasks import send_alert_email

        send_alert_email.delay(to_email, subject, text)
    except Exception:
        logger.warning("alert email enqueue failed", exc_info=True)
        return "failed"
    return "queued"


async def _open_kind(db: AsyncSession, household_id: str, kind: str) -> bool:
    result = await db.execute(
        select(HomeNotification.id).where(
            HomeNotification.household_id == household_id,
            HomeNotification.kind == kind,
            HomeNotification.read_at.is_(None),
        )
    )
    return result.first() is not None


async def _created_today(db: AsyncSession, household_id: str, kind: str) -> bool:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(HomeNotification.id).where(
            HomeNotification.household_id == household_id,
            HomeNotification.kind == kind,
            HomeNotification.created_at >= start,
        )
    )
    return result.first() is not None


async def _user_email(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    result = await db.execute(select(User.email).where(User.id == user_id))
    email = result.scalar_one_or_none()
    text = (email or "").strip()
    return text or None
