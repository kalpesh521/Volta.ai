"""JSON shape shared by the REST list, the cache, and the WebSocket."""
from __future__ import annotations

from app.modules.assistant.notifications.models import HomeNotification
from app.modules.assistant.domain.alerts import channels_for, priority_for
from app.modules.assistant.ops.schemas import NotificationOut


def notification_out(row: HomeNotification) -> NotificationOut:
    priority = row.priority if row.priority in {"critical", "warning", "info"} else priority_for(row.kind)
    return NotificationOut(
        id=row.id,
        kind=row.kind,
        priority=priority,  # type: ignore[arg-type]
        title=row.title,
        body=row.body,
        ref_id=row.ref_id,
        read=row.read_at is not None,
        email_status=row.email_status or "skipped",
        channels=list(channels_for(priority)),
        created_at=row.created_at,
    )


def notification_event(row: HomeNotification) -> dict:
    return {"type": "notification", "notification": notification_out(row).model_dump(mode="json")}
