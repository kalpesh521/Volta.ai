"""
Supervisor use case: guard and mask the question, open the conversation, build this request's agents, run the
graph, then restore, account for and map the result.

The graph is shared by every request. What is specific to one request (the home, the user, the database
session behind the knowledge and action services, the admission verdict) is built here and handed over as
`context`.

Order of one request:

    admission (dependency)   cool-down · kill switch · budget
    prepare                  input guard fast path → mask → vault → conversation → runtime
    graph                    input_guard → classify → plan → agents → synthesize → output gate → finalize
    finish                   restore allowed values · debit tokens · record strikes · map to the response

The raw question lives only inside `InputGuard.inspect` and the vault. The graph, its checkpoints, the logs and
the response all carry the guarded (normalised, masked) text.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.agents_platform.agents.bill_tariff.agent import BillTariffAgent
from app.agents_platform.agents.device_action.agent import ActionProposer, DeviceActionAgent
from app.agents_platform.agents.energy_insight import EnergyInsightAgent
from app.agents_platform.agents.insight_engine import InsightEngine
from app.agents_platform.agents.forecast.agent import ForecastAgent
from app.agents_platform.agents.forecast.weather import OpenMeteoWeather
from app.agents_platform.agents.knowledge import KnowledgeAgent
from app.agents_platform.agents.optimizer.agent import OptimizerAgent
from app.agents_platform.agents.planning_data import AuthorizedData, GatewayPlanningData
from app.agents_platform.api.admission import Admission, degraded_warning
from app.agents_platform.api.schemas import BudgetOut, SupervisorResponse
from app.agents_platform.contracts import AgentName
from app.agents_platform.conversations import ConversationRef, ConversationRepository
from app.agents_platform.guardrails.input_guard import Decision, GuardedInput
from app.agents_platform.guardrails.messages import Refusal
from app.agents_platform.guardrails.pii.masker import restore
from app.agents_platform.guardrails.policy import AuthorizedTools
from app.agents_platform.locks import UNLOCKED, ConversationBusyError
from app.agents_platform.orchestration.events import encode_sse
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.supervisor import RECURSION_LIMIT
from app.agents_platform.platform import Platform
from app.agents_platform.registry.entitlements import Entitlements
from app.agents_platform.response import results_of, to_response
from app.ai.config import AISettings
from app.ai.llm.gated import GatedLLM
from app.ai.llm.gateway import StructuredLLM
from app.core.exceptions import AppError
from app.core.metrics import ASSISTANT_SECONDS, LLM_TOKENS, SUPERVISOR_REQUESTS
from app.modules.assistant.schemas import AssistantAnswer
from app.modules.assistant.tools.gateway import EnergyDataGateway
from app.modules.assistant.tools.registry import build_energy_tools
from app.modules.energy.service import EnergyService
from app.modules.knowledge.service import KnowledgeService
from app.modules.onboarding.models import SolarSystem

logger = logging.getLogger("volta.agents.service")

# The conversation lock outlives the whole-request deadline by this much, so a request that is just finishing never
# loses its lock to a newcomer, while a crashed one still frees the conversation soon.
LOCK_MARGIN_SECONDS = 5.0


class AssistantTimeoutError(AppError):
    status_code = 504
    code = "assistant_timeout"

    def __init__(self) -> None:
        super().__init__("The assistant took too long to answer. Please try again.")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _Prepared:
    """Everything one request needs between `_prepare` and `_finish`."""

    ref: ConversationRef
    runtime: SupervisorRuntime
    inputs: dict[str, Any]
    config: dict[str, Any]
    guarded: GuardedInput
    admission: Admission
    user_id: uuid.UUID
    request_id: str
    vault_used: bool
    lock_token: str


@lru_cache
def _open_meteo(timeout_seconds: float) -> OpenMeteoWeather:
    """One client per process, so its cache is shared by every request (neighbouring homes share a weather cell)."""
    return OpenMeteoWeather(timeout_seconds=timeout_seconds)


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
        platform: Platform,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._energy = energy
        self._llm = llm
        self._graph = graph
        self._settings = settings
        self._knowledge = knowledge
        self._actions = actions
        self._conversations = conversations
        self._platform = platform
        self._clock = clock

    async def ask(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        *,
        conversation_id: str | None,
        request_id: str,
        admission: Admission,
    ) -> SupervisorResponse:
        prepared = await self._prepare(system, user_id, question, conversation_id, request_id, admission)
        started = time.perf_counter()
        try:
            try:
                async with asyncio.timeout(self._settings.AI_SUPERVISOR_DEADLINE_SECONDS):
                    state = await self._graph.ainvoke(
                        prepared.inputs, context=prepared.runtime, config=prepared.config, durability="async"
                    )
            except TimeoutError as exc:
                logger.warning("supervisor deadline exceeded request_id=%s", request_id)
                raise AssistantTimeoutError() from exc
            return await self._finish(system, prepared, state, started)
        finally:
            await self._unlock(prepared)

    async def stream(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        *,
        conversation_id: str | None,
        request_id: str,
        admission: Admission,
    ) -> AsyncIterator[str]:
        """
        Server-sent events: `status` while working, then one `final` with the checked answer.

        Failures after the stream has started cannot change the HTTP status, so they arrive as an `error`
        event. A conversation that does not exist is raised *before* the first byte, as a normal 404.
        """
        prepared = await self._prepare(system, user_id, question, conversation_id, request_id, admission)
        return self._events(system, prepared)

    async def _events(self, system: SolarSystem, prepared: _Prepared) -> AsyncIterator[str]:
        started = time.perf_counter()
        state: dict[str, Any] | None = None
        try:
            async with asyncio.timeout(self._settings.AI_SUPERVISOR_DEADLINE_SECONDS):
                async for mode, chunk in self._graph.astream(
                    prepared.inputs,
                    context=prepared.runtime,
                    config=prepared.config,
                    stream_mode=["custom", "values"],
                    durability="async",
                ):
                    if mode == "custom":
                        yield encode_sse(chunk.get("event", "status"), chunk)
                    else:
                        state = chunk
            if state is None or "answer" not in state:
                raise RuntimeError("supervisor finished without an answer")
            response = await self._finish(system, prepared, state, started)
            yield encode_sse("final", response.model_dump(mode="json"))
        except TimeoutError:
            logger.warning("supervisor deadline exceeded request_id=%s", prepared.request_id)
            yield encode_sse("error", {"code": AssistantTimeoutError.code, "message": AssistantTimeoutError().message})
        except Exception:
            logger.exception("supervisor stream failed request_id=%s", prepared.request_id)
            yield encode_sse(
                "error", {"code": "assistant_error", "message": "The assistant could not answer. Please try again."}
            )
        finally:
            await self._unlock(prepared)  # also runs when the client disconnects and the stream is closed

    # --- internals --------------------------------------------------------------------------------

    async def _prepare(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        conversation_id: str | None,
        request_id: str,
        admission: Admission,
    ) -> _Prepared:
        ref = await self._conversations.open(
            conversation_id, user_id=user_id, household_id=system.household_id
        )
        # A brand-new conversation has an id only this response knows, so nothing can race on it.
        lock_token = UNLOCKED
        if conversation_id is not None:
            token = await self._platform.locks.acquire(
                ref.thread_id, self._settings.AI_SUPERVISOR_DEADLINE_SECONDS + LOCK_MARGIN_SECONDS
            )
            if token is None:
                raise ConversationBusyError()
            lock_token = token
        try:
            return await self._build(system, user_id, question, ref, request_id, admission, lock_token)
        except BaseException:
            await self._platform.locks.release(ref.thread_id, lock_token)
            raise

    async def _unlock(self, prepared: _Prepared) -> None:
        await self._platform.locks.release(prepared.ref.thread_id, prepared.lock_token)

    async def _build(
        self,
        system: SolarSystem,
        user_id: uuid.UUID,
        question: str,
        ref: ConversationRef,
        request_id: str,
        admission: Admission,
        lock_token: str,
    ) -> _Prepared:
        guarded = self._platform.guard.inspect(question, request_id=request_id)
        # Raw values go to the vault and nowhere else. If it cannot keep them, the answer simply keeps
        # "[hidden …]" where a value would have been restored.
        vault_used = bool(guarded.tokens)
        if vault_used:
            await self._platform.vault.store(request_id, guarded.tokens)

        llm: StructuredLLM = (
            self._llm
            if admission.llm_allowed
            else GatedLLM(self._llm, reason=admission.degraded_reason or "disabled")
        )
        entitlements = self._platform.entitlements.for_tier(admission.tier)
        runtime = SupervisorRuntime(
            agents=self._build_agents(system, user_id, request_id, llm, entitlements),
            llm=llm,
            settings=self._settings,
            flags=admission.flags,
            prompts=self._platform.prompts,
            entitlements=entitlements,
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
                "prompt_version": self._platform.prompts.answer_version,
                "tier": admission.tier.value,
            },
        }
        inputs = {"question": guarded.text, "request_id": request_id, "guard": guarded.to_state()}
        return _Prepared(
            ref=ref,
            runtime=runtime,
            inputs=inputs,
            config=config,
            guarded=guarded,
            admission=admission,
            user_id=user_id,
            request_id=request_id,
            vault_used=vault_used,
            lock_token=lock_token,
        )

    def _build_agents(
        self, system: SolarSystem, user_id: uuid.UUID, request_id: str, llm: StructuredLLM, entitlements: Entitlements
    ) -> dict[AgentName, Any]:
        gateway = EnergyDataGateway(self._energy, system)
        settings = self._settings

        def tools_for(agent: AgentName, question: str):
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

            tools = build_energy_tools(
                gateway,
                trend_window_minutes=settings.AI_TREND_WINDOW_MINUTES,
                trend_history_limit=settings.AI_TREND_HISTORY_LIMIT,
                knowledge_search=search_documents,
            )
            return AuthorizedTools(agent, tools)  # gate G5: only the tools this agent may use

        engine = InsightEngine(tools_for=tools_for, llm=llm, settings=settings, now=self._clock)
        planning_data = GatewayPlanningData(gateway)  # one household, read-only; each agent sees only its scopes
        return {
            AgentName.ENERGY_INSIGHT: EnergyInsightAgent(engine, request_id=request_id),
            AgentName.KNOWLEDGE: KnowledgeAgent(engine, request_id=request_id),
            AgentName.DEVICE_ACTION: DeviceActionAgent(self._actions, system=system, user_id=user_id),
            AgentName.FORECAST: ForecastAgent(
                AuthorizedData(AgentName.FORECAST, planning_data),
                now=self._clock,
                stale_threshold_seconds=settings.AI_STALE_DATA_SECONDS,
                weather=lambda: _open_meteo(settings.AI_FORECAST_WEATHER_TIMEOUT_SECONDS)
                if settings.AI_FORECAST_WEATHER_ENABLED
                else None,
            ),
            AgentName.OPTIMIZER: OptimizerAgent(
                AuthorizedData(AgentName.OPTIMIZER, planning_data),
                now=self._clock,
                stale_threshold_seconds=settings.AI_STALE_DATA_SECONDS,
                weather=lambda: _open_meteo(settings.AI_FORECAST_WEATHER_TIMEOUT_SECONDS)
                if settings.AI_FORECAST_WEATHER_ENABLED
                else None,
            ),
            AgentName.BILL_TARIFF: BillTariffAgent(
                AuthorizedData(AgentName.BILL_TARIFF, planning_data),
                now=self._clock,
                what_if_allowed=entitlements.what_if_allowed,
            ),
        }

    async def _finish(
        self, system: SolarSystem, prepared: _Prepared, state: dict[str, Any], started: float
    ) -> SupervisorResponse:
        latency_ms = int((time.perf_counter() - started) * 1000)
        response = to_response(
            household_id=system.household_id,
            question=prepared.guarded.text,
            conversation_id=prepared.ref.id,
            state=state,  # type: ignore[arg-type]
            request_id=prepared.request_id,
            latency_ms=latency_ms,
            stale_threshold_seconds=self._settings.AI_STALE_DATA_SECONDS,
            prompt_version=self._platform.prompts.answer_version,
        )
        response = await self._account(response, prepared, state)
        await self._conversations.touch(prepared.ref.id)
        self._record(response, state)
        logger.info(
            "supervisor request_id=%s household=%s conversation=%s route=%s agents=%s intents=%s "
            "method=%s llm_used=%s fallback=%s confidence=%s tokens=%d latency_ms=%d guard=%s tier=%s",
            prepared.request_id,
            system.household_id,
            prepared.ref.id,
            response.route,
            ",".join(a.agent.value for a in response.agents),
            ",".join(response.intents),
            response.meta.classification_method,
            response.meta.llm_used,
            response.meta.fallback_reason,
            response.confidence,
            response.meta.usage.total_tokens,
            latency_ms,
            response.guard.decision if response.guard else "none",
            prepared.admission.tier.value,
        )
        return response

    async def _account(
        self, response: SupervisorResponse, prepared: _Prepared, state: dict[str, Any]
    ) -> SupervisorResponse:
        """Restore what may be shown, charge the tokens, count a refused injection, and say when the model was off."""
        platform, admission = self._platform, prepared.admission

        tokens = await platform.vault.load(prepared.request_id) if prepared.vault_used else {}
        if prepared.vault_used:
            await platform.vault.discard(prepared.request_id)
        answer = response.answer.model_dump()
        for name, value in answer.items():
            answer[name] = restore(value, tokens)

        used = response.meta.usage.total_tokens
        budget: BudgetOut | None = None
        if admission.budget is not None:  # admission read the budget settings: it is the one source of truth
            await platform.budget.debit(prepared.user_id, admission.tier, used)
            budget = BudgetOut(
                tier=admission.tier.value,
                daily_limit=admission.budget.limit,
                remaining=max(0, admission.budget.remaining - used),
                resets_in_seconds=admission.budget.seconds_until_reset,
                degraded=admission.degraded_reason is not None,
            )

        guard = state.get("guard") or {}
        if guard.get("decision") == Decision.BLOCK.value and guard.get("reason") == Refusal.INPUT_BLOCKED.value:
            await platform.abuse.record_block(prepared.user_id)

        warnings = list(response.warnings)
        if admission.degraded_reason and not response.meta.llm_used:
            warnings.append(degraded_warning(admission.degraded_reason))
        return response.model_copy(
            update={"answer": AssistantAnswer.model_validate(answer), "budget": budget, "warnings": warnings}
        )

    @staticmethod
    def _record(response: SupervisorResponse, state: dict[str, Any]) -> None:
        path = "llm" if response.meta.llm_used else "deterministic"
        SUPERVISOR_REQUESTS.labels(route=response.route, path=path).inc()
        ASSISTANT_SECONDS.labels(path=path).observe(response.meta.latency_ms / 1000)
        for call in [*state.get("llm_calls", []), *(c for r in results_of(state) for c in r.llm_calls)]:
            LLM_TOKENS.labels(model=call["model"], direction="input").inc(call["input_tokens"])
            LLM_TOKENS.labels(model=call["model"], direction="output").inc(call["output_tokens"])
