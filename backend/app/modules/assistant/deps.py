"""
FastAPI dependency providers for the assistant.

Model clients and the compiled graph are process singletons. Tests override
`get_structured_llm` with a fake so no network call is made.
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from langgraph.graph.state import CompiledStateGraph

from app.ai.config import AISettings, get_ai_settings
from app.ai.llm.factory import ChatModelFactory
from app.ai.llm.gateway import LangChainStructuredLLM, StructuredLLM
from app.modules.assistant.graph.builder import build_assistant_graph
from app.modules.assistant.service import AssistantService
from app.modules.energy.deps import get_energy_service
from app.modules.energy.service import EnergyService


def get_assistant_settings() -> AISettings:
    return get_ai_settings()


@lru_cache
def get_chat_model_factory() -> ChatModelFactory:
    return ChatModelFactory(get_ai_settings())


@lru_cache
def get_structured_llm() -> StructuredLLM:
    return LangChainStructuredLLM(get_chat_model_factory())


@lru_cache
def get_assistant_graph() -> CompiledStateGraph:
    return build_assistant_graph()


def get_assistant_service(
    energy: EnergyService = Depends(get_energy_service),
    llm: StructuredLLM = Depends(get_structured_llm),
    graph: CompiledStateGraph = Depends(get_assistant_graph),
    settings: AISettings = Depends(get_assistant_settings),
) -> AssistantService:
    return AssistantService(energy=energy, llm=llm, graph=graph, settings=settings)
