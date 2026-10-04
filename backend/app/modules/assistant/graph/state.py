"""
Graph state and per-request runtime context.

State holds only JSON-serialisable values so a LangGraph checkpointer
(conversation memory, human-in-the-loop) can be added without migration.
Request-scoped dependencies live in `AssistantRuntime`, injected through
`context_schema`, so the graph itself is compiled once per process.
"""
from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.tools import BaseTool

from app.ai.config import AISettings
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.tools.registry import ToolName

Route = Literal["generate", "fallback", "finalize"]


class ToolCallRecord(TypedDict):
    name: str
    status: Literal["ok", "unavailable", "timeout", "error"]
    latency_ms: int


class LLMCallRecord(TypedDict):
    role: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


class AssistantState(TypedDict, total=False):
    question: str
    request_id: str

    intents: list[str]
    appliance: str | None
    classification_method: str

    planned_tools: list[str]
    planned_analytics: list[str]
    planned_details: list[str]
    requires_telemetry: bool
    live_advice: bool

    tool_results: dict[str, dict[str, Any]]
    tool_calls: list[ToolCallRecord]

    context: dict[str, Any]
    prompt_context: dict[str, Any]
    prompt_analytics: dict[str, Any]
    prompt_freshness: dict[str, Any]
    context_reductions: list[str]
    analytics: dict[str, Any]
    freshness: dict[str, Any]
    route: Route
    fallback_reason: str | None

    answer: dict[str, str]
    llm_used: bool
    confidence: str

    warnings: Annotated[list[str], operator.add]
    llm_calls: Annotated[list[LLMCallRecord], operator.add]
    errors: Annotated[list[str], operator.add]


@dataclass(frozen=True)
class AssistantRuntime:
    tools: Mapping[ToolName, BaseTool]
    llm: StructuredLLM
    settings: AISettings
    now: Callable[[], datetime]
