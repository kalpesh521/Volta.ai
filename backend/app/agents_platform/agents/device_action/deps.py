"""Process-wide action graph. Its checkpointer comes from `checkpointer_provider`."""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.checkpointer import checkpointer_provider
from app.core.database import get_db
from app.modules.assistant.actions.graph import build_action_graph
from app.modules.assistant.actions.publisher import CommandPublisher, default_publisher
from app.modules.assistant.actions.service import ActionService
from app.modules.energy.deps import get_energy_service
from app.modules.energy.service import EnergyService


@lru_cache
def get_action_graph() -> CompiledStateGraph:
    """Compiled once, after the lifespan has started the checkpointer."""
    return build_action_graph(checkpointer_provider.saver)


def get_command_publisher() -> CommandPublisher:
    return default_publisher()


def get_action_service(
    db: AsyncSession = Depends(get_db),
    energy: EnergyService = Depends(get_energy_service),
    graph: CompiledStateGraph = Depends(get_action_graph),
    publisher: CommandPublisher = Depends(get_command_publisher),
) -> ActionService:
    return ActionService(db=db, energy=energy, graph=graph, publisher=publisher)
