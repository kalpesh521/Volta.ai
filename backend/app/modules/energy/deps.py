"""
FastAPI dependency providers for the energy module.

Ingest uses a shared secret (X-Ingest-Token). Read APIs use get_current_user
from core.deps — wired on the router, not here.
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import Depends, Header, Request

from app.core.config import settings
from app.core.exceptions import InvalidIngestTokenError
from app.modules.energy.service import EnergyService
from app.modules.energy.store import EnergyStore, energy_store


def get_energy_store() -> EnergyStore:
    return energy_store


def get_energy_service(
    request: Request,
    store: EnergyStore = Depends(get_energy_store),
) -> EnergyService:
    timescale = getattr(request.app.state, "timescale", None)
    redis_live = getattr(request.app.state, "redis_live", None)
    return EnergyService(store, timescale=timescale, redis_live=redis_live)


def _token_digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def require_ingest_token(
    x_ingest_token: str | None = Header(
        default=None,
        alias="X-Ingest-Token",
        description="Shared secret the simulator sends on POST /energy/ingest",
    ),
) -> None:
    """
    Constant-time compare of SHA-256 digests so token length is not leaked
    via hmac.compare_digest's equal-length requirement.
    """
    expected = (settings.INGEST_TOKEN or "").strip()
    provided = (x_ingest_token or "").strip()
    if not expected or not provided:
        raise InvalidIngestTokenError()
    if not hmac.compare_digest(_token_digest(provided), _token_digest(expected)):
        raise InvalidIngestTokenError()
