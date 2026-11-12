"""Model registry: which provider and model serves each role, and whether it can be used right now."""
from __future__ import annotations

from dataclasses import dataclass

from app.ai.config import AISettings, LLMRole
from app.ai.llm.gateway import StructuredLLM


@dataclass(frozen=True)
class ModelEntry:
    role: str
    provider: str
    model: str
    available: bool


def describe_models(settings: AISettings, llm: StructuredLLM) -> list[ModelEntry]:
    """One row per role. Roles without their own setting share the router or answer model (see `AISettings.target`)."""
    entries = []
    for role in LLMRole:
        target = settings.target(role)
        entries.append(ModelEntry(role.value, target.provider, target.model, llm.is_available(role)))
    return entries
