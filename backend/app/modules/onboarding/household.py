"""Stable household IDs for telemetry keys and simulator ticks.

Format matches energy ingest / WebSocket: ``home_`` + 12 hex chars.
Assigned once at solar-system creation and never rotated so Timescale
history stays attached to the same home.
"""
from __future__ import annotations

import re
import uuid

# Keep in lockstep with app.modules.energy.schemas._HOUSEHOLD_ID_PATTERN
HOUSEHOLD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_HOUSEHOLD_RE = re.compile(HOUSEHOLD_ID_PATTERN)


def generate_household_id() -> str:
    candidate = f"home_{uuid.uuid4().hex[:12]}"
    if not _HOUSEHOLD_RE.fullmatch(candidate):
        raise RuntimeError("generated household_id failed validation")
    return candidate


def is_valid_household_id(value: str) -> bool:
    return bool(value) and bool(_HOUSEHOLD_RE.fullmatch(value))
