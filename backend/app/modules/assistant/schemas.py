"""HTTP contracts for the assistant API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.ai.config import ai_settings


class AskRequest(BaseModel):
    question: str = Field(
        min_length=2,
        max_length=ai_settings.AI_MAX_QUESTION_CHARS,
        examples=["Should I run the washing machine now?"],
    )

    @field_validator("question", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class AssistantAnswer(BaseModel):
    observation: str
    explanation: str
    recommendation: str
    estimated_impact: str
    data_time: str


class SourceOut(BaseModel):
    document_id: str
    title: str
    doc_type: str
    page: int | None = None
    score: float
    excerpt: str


class FreshnessOut(BaseModel):
    status: Literal["fresh", "stale", "missing"]
    data_time: str | None = None
    age_seconds: int | None = None
    threshold_seconds: int


class ToolCallOut(BaseModel):
    name: str
    status: Literal["ok", "unavailable", "timeout", "error"]
    latency_ms: int


class UsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AssistantMeta(BaseModel):
    request_id: str
    llm_used: bool
    model: str | None = None
    classification_method: str
    fallback_reason: str | None = None
    prompt_version: str
    latency_ms: int
    usage: UsageOut
    tool_calls: list[ToolCallOut]


class AssistantResponse(BaseModel):
    household_id: str
    question: str
    answer: AssistantAnswer
    confidence: Literal["high", "medium", "low"]
    warnings: list[str]
    intents: list[str]
    appliance: str | None = None
    tools_used: list[str]
    analytics_used: list[str]
    data_freshness: FreshnessOut
    sources: list[SourceOut] = Field(default_factory=list)
    meta: AssistantMeta


class ModelStatusOut(BaseModel):
    provider: str
    model: str
    available: bool


class AssistantStatusOut(BaseModel):
    enabled: bool
    mode: Literal["llm", "deterministic"]
    answer_model: ModelStatusOut
    router_model: ModelStatusOut
    fallback_model: ModelStatusOut | None = None
    prompt_version: str
