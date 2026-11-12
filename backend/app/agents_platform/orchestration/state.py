"""
Supervisor graph state.

One conversation is one LangGraph thread, so this state is checkpointed after every step and
outlives a single question. That splits its keys in two:

* **memory** (`turns`) survives from question to question, capped so it cannot grow without bound.
* **per-turn keys** describe the question being answered. Reducer keys would otherwise keep
  appending across questions (a second question would see the first one's agent results), so
  `begin_turn` resets every one of them. `PER_TURN_RESET` is the single list to keep in step with
  this class; a test fails when a key is added here and not reset.

Everything is plain JSON (dicts, lists, strings, numbers). Typed models are validated at the
edges: results are dumped with `model_dump(mode="json")` on the way in.
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.types import Overwrite

MAX_STORED_TURNS = 10


class Turn(TypedDict):
    """One finished question, kept for follow-ups. No answer text: facts are always re-read live."""

    question: str
    intents: list[str]
    appliance: str | None
    agents: list[str]


def append_turns(existing: list[Turn] | None, new: list[Turn] | None) -> list[Turn]:
    return [*(existing or []), *(new or [])][-MAX_STORED_TURNS:]


class SupervisorState(TypedDict, total=False):
    # input
    question: str
    request_id: str
    # The input guard's fast-path verdict, supplied with every question and updated by the `input_guard` node.
    # Never holds raw personal data (see `GuardedInput.to_state`).
    guard: dict[str, Any]

    # memory: survives across questions in a thread
    turns: Annotated[list[Turn], append_turns]

    # per-turn
    effective_question: str
    intents: list[str]
    appliance: str | None
    classification_method: str
    plan: dict[str, Any]
    results: Annotated[list[dict[str, Any]], operator.add]
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    answer: dict[str, str]
    llm_used: bool
    attempts: int
    ungrounded: list[str]
    grounding: dict[str, Any] | None
    fallback_reason: str | None
    output_violations: list[str]
    confidence: str
    route: str


class AgentInput(TypedDict):
    """What a `Send` delivers to an agent node: its call and nothing else (least-privilege context)."""

    call: dict[str, Any]


def per_turn_reset() -> dict[str, Any]:
    """Fresh values for every per-turn key. Reducer keys use `Overwrite` to bypass their reducer."""
    return {
        "effective_question": "",
        "intents": [],
        "appliance": None,
        "classification_method": "rules",
        "plan": {},
        "results": Overwrite([]),
        "llm_calls": Overwrite([]),
        "warnings": Overwrite([]),
        "errors": Overwrite([]),
        "answer": {},
        "llm_used": False,
        "attempts": 0,
        "ungrounded": [],
        "grounding": None,
        "fallback_reason": None,
        "output_violations": [],
        "confidence": "low",
        "route": "",
    }


PERSISTENT_KEYS = frozenset({"question", "request_id", "guard", "turns"})
