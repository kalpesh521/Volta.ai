"""Redis pub/sub fan-out, with an in-process fallback for one API worker."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from redis.asyncio import from_url

from app.core.config import settings
from app.modules.assistant.notifications.redis_client import (
    mark_notification_redis_down,
    notification_channel,
    notification_redis,
)

logger = logging.getLogger("volta.notifications")


class LocalHub:
    """Same-process subscribers. Used only when Redis publish fails."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, household_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(household_id, set()).add(queue)
        return queue

    def unsubscribe(self, household_id: str, queue: asyncio.Queue) -> None:
        group = self._subscribers.get(household_id)
        if not group:
            return
        group.discard(queue)
        if not group:
            self._subscribers.pop(household_id, None)

    def publish(self, household_id: str, event: dict) -> None:
        for queue in list(self._subscribers.get(household_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("notification queue full household=%s", household_id)


local_hub = LocalHub()


async def publish(household_id: str, event: dict) -> None:
    # Local delivery covers the API process that created the alert. Redis covers
    # every other replica. The dashboard ignores a second copy of the same id.
    local_hub.publish(household_id, event)
    client = await notification_redis()
    if client is None:
        return
    try:
        await client.publish(notification_channel(household_id), json.dumps(event))
    except Exception:
        logger.warning("notification redis publish failed household=%s", household_id, exc_info=True)
        mark_notification_redis_down()


async def iter_events(household_id: str) -> AsyncIterator[dict]:
    """Subscribe on a dedicated connection so publish/cache can keep using the shared client."""
    client = await notification_redis()
    if client is None:
        return
    subscriber = from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=0.3,
        socket_timeout=0.8,
    )
    pubsub = subscriber.pubsub()
    channel = notification_channel(household_id)
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if not message or message.get("type") != "message":
                continue
            data = message.get("data")
            if not isinstance(data, str) or not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await subscriber.aclose()
