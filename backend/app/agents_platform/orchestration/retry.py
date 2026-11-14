"""
Bounded retry for specialist agents that wait on slower dependencies (tracker 3.14).

LangGraph's own `RetryPolicy` does not fit here: an agent node is a failure boundary that turns every exception
into a typed result, so the graph never sees one to retry; and a graph-level retry would restart the node with a
fresh deadline. This keeps the two properties that matter:

* **One deadline.** All attempts, and the pauses between them, run inside the agent's single deadline (the node
  wraps this call in `asyncio.wait_for`). A retry can never make a slow agent slower than its ceiling.
* **Only for failures worth another try.** An agent raises `TransientAgentError` for a dependency that failed in a
  way that may not repeat (a forecast provider 5xx, a connection reset). A bug, a refusal or a timeout is not
  retried. Device Action is never retried at all, whatever it raises: it *proposes*, and a second attempt could
  propose twice.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.agents_platform.contracts import AgentName
from app.ai.config import AISettings
from app.core.metrics import AGENT_RETRIES

logger = logging.getLogger("volta.agents.supervisor")

T = TypeVar("T")

# Agents allowed to retry. Anything not listed makes exactly one attempt.
RETRYABLE_AGENTS = frozenset({AgentName.FORECAST, AgentName.OPTIMIZER, AgentName.BILL_TARIFF})


class TransientAgentError(Exception):
    """A dependency failed in a way that may not repeat. The only error the retry loop acts on."""


def max_attempts(agent: AgentName, settings: AISettings) -> int:
    if agent not in RETRYABLE_AGENTS:
        return 1
    return max(1, settings.AI_PLANNING_AGENT_MAX_ATTEMPTS)


async def run_with_retry(
    attempt: Callable[[], Awaitable[T]],
    *,
    agent: AgentName,
    attempts: int,
    backoff_seconds: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call `attempt` up to `attempts` times, pausing `backoff * 2**n` between tries, on `TransientAgentError` only."""
    for number in range(1, attempts + 1):
        try:
            return await attempt()
        except TransientAgentError as exc:
            if number == attempts:
                raise
            AGENT_RETRIES.labels(agent=agent.value).inc()
            logger.warning("agent %s attempt %d/%d failed transiently: %s", agent.value, number, attempts, exc)
            await sleep(backoff_seconds * 2 ** (number - 1))
    raise AssertionError("unreachable: attempts is at least 1")  # pragma: no cover
