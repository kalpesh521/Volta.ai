"""
Parse and validate one AMQP telemetry body.

The worker ACKs a tick after this succeeds. Schema/JSON failures are poison
(reject → DLQ). An energy-balance warning is still accepted — same as HTTP ingest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from app.modules.energy.schemas import TelemetryRecord
from app.modules.energy.service import normalize_record

IngestAction = Literal["ack", "reject"]


@dataclass(frozen=True)
class IngestDecision:
    action: IngestAction
    record: TelemetryRecord | None
    reason: str


def process_telemetry_body(body: bytes, tolerance_kw: float) -> IngestDecision:
    if not body or not body.strip():
        return IngestDecision("reject", None, "empty_body")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return IngestDecision("reject", None, f"invalid_json:{exc.msg}")
    if not isinstance(payload, dict):
        return IngestDecision("reject", None, "payload_not_object")
    try:
        record = TelemetryRecord.model_validate(payload)
    except ValidationError as exc:
        return IngestDecision(
            "reject",
            None,
            f"validation_error:{exc.error_count()}_issues",
        )
    normalized = normalize_record(record, tolerance_kw)
    return IngestDecision("ack", normalized, "ok")
