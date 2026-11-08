"""Contracts for recommendations, in-app alerts, and automation rules."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AutomationMode = Literal[
    "suggest_only",
    "require_confirmation",
    "automatic_for_approved_devices",
    "disabled",
]
ControllableDevice = Literal[
    "water_heater",
    "washing_machine",
    "air_conditioner",
    "water_pump",
    "ev_charger",
]


class RecommendationOut(BaseModel):
    surplus_kw: float | None = None
    backup_hours: float | None = None
    estimated_savings_inr: float | None = None
    surplus_window: dict | None = None
    high_grid_import: bool = False
    abnormal_consumption: bool = False
    best_device: dict | None = None


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    priority: Literal["critical", "warning", "info"] = "info"
    title: str
    body: str
    ref_id: str | None = None
    read: bool
    email_status: str = "skipped"
    channels: list[str] = Field(default_factory=list)
    created_at: datetime


class AutomationIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    device: ControllableDevice
    command: Literal["on", "off"] = "on"
    surplus_kw_min: float | None = Field(default=None, ge=0)
    soc_percent_min: float | None = Field(default=None, ge=0, le=100)
    mode: AutomationMode = "suggest_only"


class AutomationOut(BaseModel):
    id: uuid.UUID
    name: str
    device: str
    command: str
    surplus_kw_min: float | None = None
    soc_percent_min: float | None = None
    mode: AutomationMode
    enabled: bool


class AutomationResult(BaseModel):
    rule_id: uuid.UUID
    matched: bool
    mode: AutomationMode
    proposal_id: str | None = None
    notification_id: uuid.UUID | None = None
