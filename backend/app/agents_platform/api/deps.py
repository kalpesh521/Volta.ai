"""
FastAPI wiring for the v2 assistant.

The supervisor graph is a process singleton, compiled once the lifespan has started the checkpointer.
Tests override `get_structured_llm` (no network) and `get_db_scope` (the test transaction).
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, Request
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.conversations import ConversationRepository
from app.agents_platform.orchestration.supervisor import build_supervisor
from app.agents_platform.service import SupervisorService
from app.ai.checkpointer import checkpointer_provider
from app.ai.config import AISettings
from app.ai.llm.gateway import StructuredLLM
from app.core.database import AsyncSessionLocal, get_db
from app.modules.assistant.actions.deps import get_action_graph, get_command_publisher
from app.modules.assistant.actions.service import ActionService
from app.modules.assistant.deps import get_assistant_settings, get_structured_llm
from app.modules.energy.deps import get_energy_service, get_energy_store
from app.modules.knowledge.deps import build_knowledge_service, get_embedder

DbScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@lru_cache
def get_supervisor_graph() -> CompiledStateGraph:
    """Compiled once, after the lifespan has started the checkpointer."""
    return build_supervisor(checkpointer_provider.saver)


@asynccontextmanager
async def _session_scope():
    """A session that commits on a clean exit. For streaming responses, whose body runs after `get_db` exits."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_db_scope() -> DbScope:
    return _session_scope


@dataclass(frozen=True)
class SupervisorServiceFactory:
    """Builds a service around a session the caller owns."""

    request: Request
    llm: StructuredLLM
    settings: AISettings
    graph: CompiledStateGraph

    def __call__(self, db: AsyncSession) -> SupervisorService:
        energy = get_energy_service(self.request, get_energy_store())
        actions = ActionService(
            db=db,
            energy=energy,
            graph=get_action_graph(),
            publisher=get_command_publisher(),
        )
        return SupervisorService(
            energy=energy,
            llm=self.llm,
            graph=self.graph,
            settings=self.settings,
            knowledge=build_knowledge_service(db, self.settings, get_embedder()),
            actions=actions,
            conversations=ConversationRepository(db),
        )


def get_service_factory(
    request: Request,
    llm: StructuredLLM = Depends(get_structured_llm),
    settings: AISettings = Depends(get_assistant_settings),
    graph: CompiledStateGraph = Depends(get_supervisor_graph),
) -> SupervisorServiceFactory:
    return SupervisorServiceFactory(request=request, llm=llm, settings=settings, graph=graph)


def get_supervisor_service(
    factory: SupervisorServiceFactory = Depends(get_service_factory),
    db: AsyncSession = Depends(get_db),
) -> SupervisorService:
    return factory(db)
