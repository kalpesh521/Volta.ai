"""Authenticated notification socket. Redis pub/sub reaches every API replica."""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select

from app.core.config import settings
from app.core.metrics import track_ws_connection
from app.core.database import AsyncSessionLocal
from app.modules.assistant.actions.models import HomeNotification
from app.modules.assistant.notifications.bus import iter_events, local_hub
from app.modules.assistant.notifications.serialize import notification_out
from app.modules.energy.ws import (
    _HOUSEHOLD_RE,
    _origin_allowed,
    _resolve_household_for_socket,
    _user_from_token,
)

logger = logging.getLogger("volta.notifications")

router = APIRouter(tags=["notifications"])


async def load_unread(user_id: uuid.UUID, household_id: str) -> tuple[int, list[dict]]:
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(HomeNotification)
            .where(
                HomeNotification.user_id == user_id,
                HomeNotification.household_id == household_id,
                HomeNotification.read_at.is_(None),
            )
        )
        result = await session.execute(
            select(HomeNotification)
            .where(
                HomeNotification.user_id == user_id,
                HomeNotification.household_id == household_id,
                HomeNotification.read_at.is_(None),
            )
            .order_by(HomeNotification.created_at.desc())
            .limit(8)
        )
        items = [notification_out(row).model_dump(mode="json") for row in result.scalars()]
    return int(total or 0), items


@router.websocket("/ws/notifications")
async def notification_socket(
    websocket: WebSocket,
    token: str = "",
    household_id: str = "",
) -> None:
    """
    Query: `token` (user access JWT) and optional `household_id`.
    Sends `{type: hello|notification|ping|error}`.
    """
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    if household_id and not _HOUSEHOLD_RE.fullmatch(household_id):
        await websocket.send_json({"type": "error", "code": "invalid_household"})
        await websocket.close(code=1008)
        return

    user = await _user_from_token(token)
    if user is None:
        await websocket.send_json({"type": "error", "code": "invalid_token"})
        await websocket.close(code=4401)
        return

    resolved, household_error = await _resolve_household_for_socket(user, household_id)
    if not resolved:
        await websocket.send_json({"type": "error", "code": household_error or "household_forbidden"})
        await websocket.close(code=4403)
        return
    household_id = resolved

    queue = local_hub.subscribe(household_id)
    stop = asyncio.Event()
    try:
        try:
            unread_count, items = await load_unread(user.id, household_id)
        except Exception:
            logger.exception("notification snapshot failed household=%s", household_id)
            unread_count, items = 0, []
        await websocket.send_json(
            {
                "type": "hello",
                "household_id": household_id,
                "user_id": str(user.id),
                "unread_count": unread_count,
                "notifications": items,
            }
        )

        async def _heartbeat() -> None:
            interval = max(5, settings.WS_HEARTBEAT_SECONDS)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        stop.set()
                        return

        async def _pump_local() -> None:
            while not stop.is_set():
                getter = asyncio.create_task(queue.get())
                stopper = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait(
                    {getter, stopper}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                if stop.is_set() or stopper in done:
                    return
                await websocket.send_json(getter.result())

        async def _pump_redis() -> None:
            try:
                async for event in iter_events(household_id):
                    if stop.is_set():
                        return
                    await websocket.send_json(event)
            except Exception:
                logger.exception("notification redis subscribe failed household=%s", household_id)

        async def _client_watch() -> None:
            try:
                while not stop.is_set():
                    message = await websocket.receive_text()
                    if message.strip().lower() in {"close", "stop"}:
                        stop.set()
                        return
            except WebSocketDisconnect:
                stop.set()
            except Exception:
                stop.set()

        with track_ws_connection("notifications"):
            tasks = [
                asyncio.create_task(_heartbeat()),
                asyncio.create_task(_pump_local()),
                asyncio.create_task(_pump_redis()),
                asyncio.create_task(_client_watch()),
            ]
            try:
                await stop.wait()
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        local_hub.unsubscribe(household_id, queue)
        try:
            await websocket.close()
        except Exception:
            return
