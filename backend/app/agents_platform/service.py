"""
Supervisor use case: open the conversation, build this request's agents, run the graph, map the result.

The graph is shared by every request. What is specific to one request (the home, the user, the database
session behind the knowledge and action services) is built here and handed over as `context`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.agents_platform.agents.device_action import ActionProposer, DeviceActionAgent
from app.agents_platform.agents.energy_insight import EnergyInsightAgent
from app.agents_platform.agents.insight_engine import InsightEngine
from app.agents_platform.agents.knowledge import KnowledgeAgent
from app.agents_platform.api.schemas import SupervisorResponse
from app.agents_platform.contracts import AgentName
from app.agents_platform.conversations import ConversationRef, ConversationRepository
from app.agents_platform.orchestration.events import encode_sse
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.supervisor import RECURSION_LIMIT
from app.agents_platform.response import results_of, to_response
from app.ai.config import AISettings
from app.ai.llm.gateway import StructuredLLM
from app.core.exceptions import AppError
from app.core.metrics import ASSISTANT_SECONDS, LLM_TOKENS, SUPERVISOR_REQUESTS
from app.modules.assistant.prompts import PROMPT_VERSION
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.assistant.tools.registry import build_energy_tools
from app.modules.energy.service import EnergyService
from app.modules.knowledge.service import KnowledgeService
from app.modules.onboarding.models import SolarSystem

logger = logging.getLogger("volta.agents.service")


class AssistantTimeoutError(AppError):
    status_code = 504
    code = "assistant_timeout"

    def __init__(self) -> None:
        super().__init__("The assistant took too long to answer. Please try again.")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SupervisorService:
    def __init__(
        self,
        *,
        energy: EnergyService,
        llm: StructuredLLM,
        graph: CompiledStateGraph,
        settings: AISettings,
        knowledge: KnowledgeService | None,
        actions: ActionProposer,
        conversations: ConversationRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._energy = energy
        self._llm = llm
        self._graph = graph
        self._settings = settings
        self._knowledge = knowledge
        self._actions = actions
        self._conversations = conversations
        self._clock = clock

    async def ask(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        *,
        conversation_id: str | None,
        request_id: str,
    ) -> SupervisorResponse:
        ref, runtime, inputs, config = await self._prepare(
            system, user_id, question, conversation_id, request_id
        )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.AI_SUPERVISOR_DEADLINE_SECONDS):
                state = await self._graph.ainvoke(
                    inputs, context=runtime, config=config, durability="async"
                )
        except TimeoutError as exc:
            logger.warning("supervisor deadline exceeded request_id=%s", request_id)
            raise AssistantTimeoutError() from exc
        return await self._finish(system, question, ref, state, request_id, started)

    async def stream(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        *,
        conversation_id: str | None,
        request_id: str,
    ) -> AsyncIterator[str]:
        """
        Server-sent events: `status` while working, then one `final` with the checked answer.

        Failures after the stream has started cannot change the HTTP status, so they arrive as an `error`
        event. A conversation that does not exist is raised *before* the first byte, as a normal 404.
        """
        ref, runtime, inputs, config = await self._prepare(
            system, user_id, question, conversation_id, request_id
        )
        return self._events(system, question, ref, runtime, inputs, config, request_id)

    async def _events(
        self,
        system: SolarSystem,
        question: str,
        ref: ConversationRef,
        runtime: SupervisorRuntime,
        inputs: dict[str, Any],
        config: dict[str, Any],
        request_id: str,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        state: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self._settings.AI_SUPERVISOR_DEADLINE_SECONDS):
                async for mode, chunk in self._graph.astream(
                    inputs,
                    context=runtime,
                    config=config,
                    stream_mode=["custom", "values"],
                    durability="async",
                ):
                    if mode == "custom":
                        yield encode_sse(chunk.get("event", "status"), chunk)
                    else:
                        state = chunk
            if state is None or "answer" not in state:
                raise RuntimeError("supervisor finished without an answer")
            response = await self._finish(system, question, ref, state, request_id, started)
            yield encode_sse("final", response.model_dump(mode="json"))
        except TimeoutError:
            logger.warning("supervisor deadline exceeded request_id=%s", request_id)
            yield encode_sse("error", {"code": AssistantTimeoutError.code, "message": AssistantTimeoutError().message})
        except Exception:
            logger.exception("supervisor stream failed request_id=%s", request_id)
            yield encode_sse(
                "error", {"code": "assistant_error", "message": "The assistant could not answer. Please try again."}
            )

    # --- internals --------------------------------------------------------------------------------

    async def _prepare(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        conversation_id: str | None,
        request_id: str,
    ) -> tuple[ConversationRef, SupervisorRuntime, dict[str, Any], dict[str, Any]]:
        ref = await self._conversations.open(
            conversation_id, user_id=user_id, household_id=system.household_id
        )
        runtime = SupervisorRuntime(
            agents=self._build_agents(system, user_id, request_id),
            llm=self._llm,
            settings=self._settings,
        )
        config = {
            "configurable": {"thread_id": ref.thread_id},
            "recursion_limit": RECURSION_LIMIT,
            "run_name": "suryaa_supervisor",
            "tags": ["assistant", "supervisor"],
            "metadata": {
                "request_id": request_id,
                "household_id": system.household_id,
                "conversation_id": ref.id,
                "prompt_version": PROMPT_VERSION,
            },
        }
        return ref, runtime, {"question": question, "request_id": request_id}, config

    def _build_agents(self, system: SolarSystem, user_id: uuid.UUID, request_id: str) -> dict[AgentName, Any]:
        gateway = EnergyDataGateway(self._energy, system)
        settings = self._settings

        def tools_for(question: str):
            async def search_documents() -> dict[str, Any]:
                if self._knowledge is None:
                    return {"available": False, "reason": "No knowledge base is configured for this request."}
                grid = system.__dict__.get("grid_config")
                return await self._knowledge.retrieve_for_assistant(
                    user_id=system.user_id,
                    question=question,
                    brand=system.inverter_brand,
                    discom=grid.discom if grid is not None else None,
                )

            return build_energy_tools(
                gateway,
                trend_window_minutes=settings.AI_TREND_WINDOW_MINUTES,
                trend_history_limit=settings.AI_TREND_HISTORY_LIMIT,
                knowledge_search=search_documents,
            )

        engine = InsightEngine(tools_for=tools_for, llm=self._llm, settings=settings, now=self._clock)
        return {
            AgentName.ENERGY_INSIGHT: EnergyInsightAgent(engine, request_id=request_id),
            AgentName.KNOWLEDGE: KnowledgeAgent(engine, request_id=request_id),
            AgentName.DEVICE_ACTION: DeviceActionAgent(self._actions, system=system, user_id=user_id),
        }

    async def _finish(
        self,
        system: SolarSystem,
        question: str,
        ref: ConversationRef,
        state: dict[str, Any],
        request_id: str,
        started: float,
    ) -> SupervisorResponse:
        latency_ms = int((time.perf_counter() - started) * 1000)
        response = to_response(
            household_id=system.household_id,
            question=question,
            conversation_id=ref.id,
            state=state,  # type: ignore[arg-type]
            request_id=request_id,
            latency_ms=latency_ms,
            stale_threshold_seconds=self._settings.AI_STALE_DATA_SECONDS,
        )
        await self._conversations.touch(ref.id)
        self._record(response, state)
        logger.info(
            "supervisor request_id=%s household=%s conversation=%s route=%s agents=%s intents=%s "
            "method=%s llm_used=%s fallback=%s confidence=%s tokens=%d latency_ms=%d question_chars=%d",
            request_id,
            system.household_id,
            ref.id,
            response.route,
            ",".join(a.agent.value for a in response.agents),
            ",".join(response.intents),
            response.meta.classification_method,
            response.meta.llm_used,
            response.meta.fallback_reason,
            response.confidence,
            response.meta.usage.total_tokens,
            latency_ms,
            len(question),
        )
        return response

    @staticmethod
    def _record(response: SupervisorResponse, state: dict[str, Any]) -> None:
        path = "llm" if response.meta.llm_used else "deterministic"
        SUPERVISOR_REQUESTS.labels(route=response.route, path=path).inc()
        ASSISTANT_SECONDS.labels(path=path).observe(response.meta.latency_ms / 1000)
        for call in [*state.get("llm_calls", []), *(c for r in results_of(state) for c in r.llm_calls)]:
            LLM_TOKENS.labels(model=call["model"], direction="input").inc(call["input_tokens"])
            LLM_TOKENS.labels(model=call["model"], direction="output").inc(call["output_tokens"])
