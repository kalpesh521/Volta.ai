"""Redis live snapshot + pub/sub fan-out for WebSocket replicas."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from redis.asyncio import Redis, from_url

from app.core.config import settings
from app.modules.energy.schemas import TelemetryRecord

logger = logging.getLogger("volta.redis")


class RedisLiveStore:
    def __init__(self, client: Redis | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    def live_key(self, household_id: str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}{household_id}"

    def channel(self, household_id: str) -> str:
        return f"{settings.REDIS_KEY_PREFIX}ch:{household_id}"

    async def connect(self) -> None:
        if self._client is None:
            self._client = from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        assert self._client is not None
        return bool(await self._client.ping())

    async def set_and_publish(self, record: TelemetryRecord) -> None:
        assert self._client is not None
        body = record.model_dump_json()
        hid = record.household_id
        await self._client.set(
            self.live_key(hid),
            body,
            ex=max(30, settings.REDIS_LIVE_TTL_SECONDS),
        )
        await self._client.publish(self.channel(hid), body)

    async def get_latest(self, household_id: str) -> TelemetryRecord | None:
        assert self._client is not None
        raw = await self._client.get(self.live_key(household_id))
        if not raw:
            return None
        return TelemetryRecord.model_validate_json(raw)

    async def subscribe(self, household_id: str) -> AsyncIterator[str]:
        assert self._client is not None
        pubsub = self._client.pubsub()
        await pubsub.subscribe(self.channel(household_id))
        try:
            async for message in pubsub.listen():
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, str) and data:
                    yield data
        finally:
            await pubsub.unsubscribe(self.channel(household_id))
            await pubsub.aclose()
