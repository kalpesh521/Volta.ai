"""Authenticated WebSocket live feed. Redis pub/sub fans out across API replicas."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.security import TokenType, decode_token
from app.modules.auth.repositories.user_repository import UserRepository
from app.modules.energy.schemas import _HOUSEHOLD_ID_PATTERN
from app.modules.energy.service import stamp_onboarding_place, to_live
from app.modules.onboarding.repository import OnboardingRepository

logger = logging.getLogger("volta.ws")

router = APIRouter(tags=["energy-live"])
_HOUSEHOLD_RE = re.compile(_HOUSEHOLD_ID_PATTERN)


_LEGACY_HOUSEHOLDS = frozenset({"home_001"})


async def _user_from_token(token: str):
    raw = (token or "").strip()
    if not raw:
        return None
    try:
        payload = decode_token(raw)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != TokenType.ACCESS:
        return None
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    async with AsyncSessionLocal() as session:
        user = await UserRepository(session).get_by_id(user_id)
        if user is None or not user.is_active:
            return None
        return user


async def _resolve_household_for_socket(user, household_id: str) -> tuple[str | None, str]:
    """
    Bind the socket to a household the caller is allowed to watch.

    Returns (household_id, error_code). error_code is "" on success.

    omitted / home_001  → primary completed home from the JWT, else any
                          completed home; demo home_001 only when the user
                          has no onboarding row yet (non-production)
    any other id        → must own it and have finished onboarding
    """
    requested = (household_id or "").strip()
    demo_alias = not requested or requested in _LEGACY_HOUSEHOLDS
    production = settings.ENVIRONMENT.lower() == "production"

    async with AsyncSessionLocal() as session:
        repo = OnboardingRepository(session)
        homes = await repo.list_systems_by_user(user.id)
        primary = next((home for home in homes if home.is_primary), None)
        if primary is None and homes:
            primary = homes[0]

        if requested and not demo_alias:
            system = await repo.get_system_by_household(requested)
            if system is None or system.user_id != user.id:
                return None, "household_forbidden"
            if not system.is_complete:
                return None, "onboarding_incomplete"
            return system.household_id, ""

        if primary is not None:
            if not primary.is_complete:
                return None, "onboarding_incomplete"
            return primary.household_id, ""
        if homes:
            return None, "onboarding_incomplete"
        if not production and demo_alias:
            return requested or "home_001", ""
        return None, "onboarding_incomplete"


async def _onboarding_location(household_id: str) -> str | None:
    try:
        async with AsyncSessionLocal() as session:
            system = await OnboardingRepository(session).get_system_by_household(
                household_id
            )
            if system is None:
                return None
            text = (system.location or "").strip()
            return text or None
    except Exception:
        logger.debug(
            "onboarding location lookup failed household=%s",
            household_id,
            exc_info=True,
        )
        return None


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    return origin in settings.CORS_ORIGINS


@router.websocket("/ws/energy")
async def energy_live_socket(
    websocket: WebSocket,
    token: str = "",
    household_id: str = "",
) -> None:
    """
    Query: `token` (user access JWT) and optional `household_id`.
    Omit household_id to use the primary completed home from the login token.
    Sends `{type: hello|telemetry|ping|error, ...}`.
    """
    origin = websocket.headers.get("origin")
    if not _origin_allowed(origin):
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
        await websocket.send_json(
            {"type": "error", "code": household_error or "household_forbidden"}
        )
        await websocket.close(code=4403)
        return
    household_id = resolved

    redis_live = getattr(websocket.app.state, "redis_live", None)
    timescale = getattr(websocket.app.state, "timescale", None)
    from app.modules.energy.store import energy_store

    latest = None
    if redis_live is not None:
        latest = await redis_live.get_latest(household_id)
    if latest is None and timescale is not None:
        latest = await timescale.get_latest(household_id)
    if latest is None:
        latest = await energy_store.get_latest(household_id)

    onboarding_location = await _onboarding_location(household_id)
    await websocket.send_json(
        {
            "type": "hello",
            "household_id": household_id,
            "user_id": str(user.id),
            "location": onboarding_location,
        }
    )
    if latest is not None:
        latest = stamp_onboarding_place(latest, onboarding_location)
        await websocket.send_json(
            {
                "type": "telemetry",
                "record": latest.model_dump(mode="json"),
                "live": to_live(latest).model_dump(mode="json"),
            }
        )

    stop = asyncio.Event()

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

    async def _pump_redis() -> None:
        if redis_live is None:
            await stop.wait()
            return
        try:
            async for body in redis_live.subscribe(household_id):
                if stop.is_set():
                    break
                from app.modules.energy.schemas import TelemetryRecord

                record = stamp_onboarding_place(
                    TelemetryRecord.model_validate_json(body),
                    onboarding_location,
                )
                await websocket.send_json(
                    {
                        "type": "telemetry",
                        "record": record.model_dump(mode="json"),
                        "live": to_live(record).model_dump(mode="json"),
                    }
                )
        except Exception:
            logger.exception("websocket redis subscribe failed household=%s", household_id)
            stop.set()

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

    tasks = [
        asyncio.create_task(_heartbeat()),
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
        try:
            await websocket.close()
        except Exception:
            pass
