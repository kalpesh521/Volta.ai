"""
Router cascade: rules first, router LLM only when the rules are not confident.

Used by the v1 graph's `classify` node and by the v2 supervisor, so both route a question the same way.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from langchain_core.runnables import RunnableConfig

from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.domain.intents import (
    ClassificationMethod,
    IntentClassification,
    IntentMatch,
    classify_by_rules,
    from_llm,
)
from app.modules.assistant.graph.state import LLMCallRecord, llm_record
from app.modules.assistant.prompts import router_messages

logger = logging.getLogger("volta.assistant.routing")


@dataclass(frozen=True)
class RoutingOutcome:
    match: IntentMatch
    llm_calls: list[LLMCallRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def route_question(
    question: str,
    *,
    llm: StructuredLLM,
    config: RunnableConfig | None = None,
    rules: IntentMatch | None = None,
    history: Sequence[str] = (),
) -> RoutingOutcome:
    """Classify with rules; ask the router LLM only when they are unsure. Never raises on LLM failure."""
    match = rules if rules is not None else classify_by_rules(question)
    if match.confident:
        return RoutingOutcome(match)
    if not llm.is_available(LLMRole.ROUTER):
        return RoutingOutcome(replace(match, method=ClassificationMethod.DEFAULT))
    try:
        result = await llm.ainvoke(
            LLMRole.ROUTER, IntentClassification, router_messages(question, history), config=config
        )
    except LLMInvocationError as exc:
        logger.warning("router LLM failed, using rules: %s", exc)
        return RoutingOutcome(replace(match, method=ClassificationMethod.DEFAULT), errors=[str(exc)])
    return RoutingOutcome(
        from_llm(result.output, match.appliance), llm_calls=[llm_record(LLMRole.ROUTER, result)]
    )
