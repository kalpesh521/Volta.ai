"""HTTP contracts for document upload, review, and citations."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.knowledge.enums import DocType, ReviewStatus, Scope


class BillOut(BaseModel):
    units_kwh: float | None = None
    tariff_category: str | None = None
    sanctioned_load_kw: float | None = None
    amount_inr: float | None = None


class ReviewOut(BaseModel):
    decision: str
    note: str | None = None
    created_at: datetime


class VersionOut(BaseModel):
    id: uuid.UUID
    version_number: int
    status: str
    title: str
    effective_date: date | None = None
    created_at: datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    scope: Scope
    doc_type: DocType
    status: ReviewStatus
    title: str
    brand: str | None = None
    discom: str | None = None
    effective_date: date | None = None
    version_group_id: uuid.UUID
    version_number: int
    page_count: int
    chunk_count: int
    byte_size: int
    created_at: datetime
    bill: BillOut | None = None
    reviews: list[ReviewOut] = Field(default_factory=list)
    versions: list[VersionOut] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)
