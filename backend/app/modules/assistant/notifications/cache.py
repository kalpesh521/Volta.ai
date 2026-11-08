"""Short-lived cache of the notification list. Postgres remains the source of truth."""
from __future__ import annotations

import json
import logging

from app.core.config import settings
from app.modules.assistant.notifications.redis_client import (
    mark_notification_redis_down,
    notification_list_key,
    notification_redis,
)

logger = logging.getLogger("volta.notifications")


class NotificationCache:
    async def get_list(self, user_id: str, household_id: str) -> list[dict] | None:
        client = await notification_redis()
        if client is None:
            return None
        try:
            raw = await client.get(notification_list_key(user_id, household_id))
        except Exception:
            logger.warning("notification cache read failed", exc_info=True)
            mark_notification_redis_down()
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, list) else None

    async def set_list(self, user_id: str, household_id: str, rows: list[dict]) -> None:
        client = await notification_redis()
        if client is None:
            return
        try:
            await client.set(
                notification_list_key(user_id, household_id),
                json.dumps(rows),
                ex=max(5, settings.NOTIFICATION_CACHE_TTL_SECONDS),
            )
        except Exception:
            logger.warning("notification cache write failed", exc_info=True)
            mark_notification_redis_down()

    async def invalidate(self, user_id: str, household_id: str) -> None:
        client = await notification_redis()
        if client is None:
            return
        try:
            await client.delete(notification_list_key(user_id, household_id))
        except Exception:
            logger.warning("notification cache invalidate failed", exc_info=True)
            mark_notification_redis_down()


notification_cache = NotificationCache()
