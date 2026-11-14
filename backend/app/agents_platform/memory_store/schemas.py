"""HTTP payloads of the memory API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents_platform.memory_store.models import MemoryKind

# A hard bound on what the endpoint parses at all. The real, configurable limit (AI_MEMORY_ITEM_MAX_CHARS) is
# enforced by the service, with a clear error message.
_PARSE_LIMIT = 2000


class MemoryItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: MemoryKind
    text: str
    source: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class MemoryListOut(BaseModel):
    household_id: str
    items: list[MemoryItemOut]
    count: int
    max_items: int
    item_max_chars: int


class MemoryCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=_PARSE_LIMIT)
    kind: MemoryKind | None = Field(default=None, description="Omit to let Suryaa guess; you can edit it later.")
    expires_at: datetime | None = Field(default=None, description="Forget this automatically after this time.")


class MemoryUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=_PARSE_LIMIT)
    kind: MemoryKind | None = None
    # Sending `null` explicitly removes the expiry; leaving the field out keeps it.
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "MemoryUpdateIn":
        if not self.model_fields_set:
            raise ValueError("Send at least one of text, kind, expires_at.")
        if "text" in self.model_fields_set and self.text is None:
            raise ValueError("text cannot be null.")
        if "kind" in self.model_fields_set and self.kind is None:
            raise ValueError("kind cannot be null.")
        return self


class MemoryCreateOut(BaseModel):
    item: MemoryItemOut
    # False when the same fact was already saved: nothing new was stored.
    created: bool
    # Classes of personal data that were replaced by tokens before saving ("EMAIL", "PHONE", ...). Counts only.
    masked: list[str] = Field(default_factory=list)


class MemoryDeleteAllOut(BaseModel):
    deleted: int


class MemoryExportOut(BaseModel):
    exported_at: datetime
    household_id: str
    count: int
    items: list[MemoryItemOut]
