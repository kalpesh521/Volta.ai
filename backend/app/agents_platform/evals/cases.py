"""
Trajectory eval cases, stored as JSON Lines in `trajectories.jsonl`.

A trajectory case asserts the *path* a question takes, not only the final text: which agents ran, which route
the plan took, how the question was classified, and how many model calls it cost. A multi-agent answer can be
fluent and still wrong for the wrong reason (one agent skipped, an extra model call, a follow-up routed as a
new topic). Those regressions never show up in a text comparison.

One case per line. A case is one conversation, so a follow-up is a second turn:

    {"id": "followup-battery", "tags": ["memory"], "turns": [
       {"question": "Why is the battery discharging?", "agents": ["energy_insight"], "route": "single",
        "intents": ["battery"], "method": "rules"},
       {"question": "why?", "agents": ["energy_insight"], "route": "single",
        "intents": ["battery"], "method": "memory"}]}

Turn fields: `question`, `agents` (exact set), `route` (single | multi | exclusive | refusal) are required.
`intents` (exact, ordered), `method` (rules | memory | llm), `router_intent` (what the stub router model
answers when the rules are not confident), and `llm` ({"router": n, "answer": n, "guard": n} exact call counts)
are optional. `guard` (allow | review | block) asserts the input guard's final decision. `classifier`
(injection | clean | unavailable) turns the grey-zone classifier on and sets what the stub guard model answers.
`offer` is the device command expected to be offered beside an answered question (message = question + command); it
must come back in `offered_command` and in the answer, and Device Action must not run.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.agents_platform.contracts import AgentName
from app.agents_platform.orchestration.planning_intents import PlanningIntent
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.evals.cases import EvalDatasetError

DEFAULT_TRAJECTORIES_PATH = Path(__file__).with_name("trajectories.jsonl")
ROUTES = frozenset({"single", "multi", "exclusive", "refusal"})
METHODS = frozenset({"rules", "memory", "llm", "default"})
GUARD_DECISIONS = frozenset({"allow", "review", "block"})
CLASSIFIER_OUTCOMES = frozenset({"injection", "clean", "unavailable"})
_TURN_KEYS = frozenset(
    {"question", "agents", "route", "intents", "method", "router_intent", "llm", "guard", "classifier", "offer"}
)
_REQUIRED_TURN_KEYS = frozenset({"question", "agents", "route"})


@dataclass(frozen=True)
class TurnExpectation:
    question: str
    agents: frozenset[str]
    route: str
    intents: tuple[str, ...] | None = None
    method: str | None = None
    router_intent: str | None = None
    llm_router_calls: int | None = None
    llm_answer_calls: int | None = None
    llm_guard_calls: int | None = None
    guard: str | None = None  # the input guard's final decision: allow | review | block
    classifier: str | None = None  # what the grey-zone classifier answers: injection | clean | unavailable
    offer: str | None = None  # the device command offered beside an answered question ("explain and do")


@dataclass(frozen=True)
class TrajectoryCase:
    id: str
    tags: frozenset[str]
    turns: tuple[TurnExpectation, ...]


def parse_trajectories(lines: Iterable[str], *, source: str = "<memory>") -> tuple[TrajectoryCase, ...]:
    cases: list[TrajectoryCase] = []
    seen: set[str] = set()
    for number, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        where = f"{source}:{number}"
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{where}: not valid JSON ({exc.msg})") from exc
        case = _build_case(row, where)
        if case.id in seen:
            raise EvalDatasetError(f"{where}: duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return tuple(cases)


def load_trajectories(path: Path = DEFAULT_TRAJECTORIES_PATH) -> tuple[TrajectoryCase, ...]:
    with path.open(encoding="utf-8") as handle:
        return parse_trajectories(handle, source=path.name)


def _build_case(row: object, where: str) -> TrajectoryCase:
    if not isinstance(row, dict):
        raise EvalDatasetError(f"{where}: each line must be a JSON object")
    unknown = set(row) - {"id", "tags", "turns"}
    if unknown:
        raise EvalDatasetError(f"{where}: unknown keys {sorted(unknown)}")
    case_id = row.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise EvalDatasetError(f"{where}: 'id' must be a non-empty string")
    tags = row.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise EvalDatasetError(f"{where}: 'tags' must be a list of strings")
    turns = row.get("turns")
    if not isinstance(turns, list) or not turns:
        raise EvalDatasetError(f"{where}: 'turns' must be a non-empty list")
    return TrajectoryCase(
        id=case_id,
        tags=frozenset(tags),
        turns=tuple(_build_turn(turn, f"{where} turn {i}") for i, turn in enumerate(turns, start=1)),
    )


def _build_turn(turn: object, where: str) -> TurnExpectation:
    if not isinstance(turn, dict):
        raise EvalDatasetError(f"{where}: a turn must be a JSON object")
    if missing := _REQUIRED_TURN_KEYS - set(turn):
        raise EvalDatasetError(f"{where}: missing {sorted(missing)}")
    if unknown := set(turn) - _TURN_KEYS:
        raise EvalDatasetError(f"{where}: unknown keys {sorted(unknown)}")

    question = turn["question"]
    if not isinstance(question, str) or not question.strip():
        raise EvalDatasetError(f"{where}: 'question' must be a non-empty string")
    agents = turn["agents"]
    valid_agents = {a.value for a in AgentName}
    if not isinstance(agents, list) or not set(agents) <= valid_agents or len(set(agents)) != len(agents):
        raise EvalDatasetError(f"{where}: 'agents' must be distinct names from {sorted(valid_agents)}")
    if turn["route"] not in ROUTES:
        raise EvalDatasetError(f"{where}: 'route' must be one of {sorted(ROUTES)}")
    if (turn["route"] == "refusal") != (not agents):
        raise EvalDatasetError(f"{where}: a refusal runs no agent, and any other route runs at least one")
    if (turn["route"] == "multi") != (len(agents) > 1):
        raise EvalDatasetError(f"{where}: 'multi' means more than one agent, and only that")

    intents = turn.get("intents")
    valid_intents = {i.value for i in Intent} | {i.value for i in PlanningIntent}
    if intents is not None and (not isinstance(intents, list) or not set(intents) <= valid_intents):
        raise EvalDatasetError(f"{where}: 'intents' must be a list of known intents")
    method = turn.get("method")
    if method is not None and method not in METHODS:
        raise EvalDatasetError(f"{where}: 'method' must be one of {sorted(METHODS)}")
    router_intent = turn.get("router_intent")
    if router_intent is not None and router_intent not in valid_intents:
        raise EvalDatasetError(f"{where}: 'router_intent' must be a known intent")
    llm = turn.get("llm", {})
    if not isinstance(llm, dict) or set(llm) - {"router", "answer", "guard"} or not all(
        isinstance(v, int) and v >= 0 for v in llm.values()
    ):
        raise EvalDatasetError(f"{where}: 'llm' must map router/answer/guard to non-negative integers")
    guard = turn.get("guard")
    if guard is not None and guard not in GUARD_DECISIONS:
        raise EvalDatasetError(f"{where}: 'guard' must be one of {sorted(GUARD_DECISIONS)}")
    if guard == "block" and turn["route"] != "refusal":
        raise EvalDatasetError(f"{where}: a blocked input is a refusal")
    classifier = turn.get("classifier")
    if classifier is not None and classifier not in CLASSIFIER_OUTCOMES:
        raise EvalDatasetError(f"{where}: 'classifier' must be one of {sorted(CLASSIFIER_OUTCOMES)}")

    offer = turn.get("offer")
    if offer is not None and (not isinstance(offer, str) or not offer.strip()):
        raise EvalDatasetError(f"{where}: 'offer' must be the offered command text")
    if offer is not None and "device_action" in agents:
        raise EvalDatasetError(f"{where}: an offered command is never run, so device_action cannot also run")

    return TurnExpectation(
        question=question,
        agents=frozenset(agents),
        route=turn["route"],
        intents=tuple(intents) if intents is not None else None,
        method=method,
        router_intent=router_intent,
        llm_router_calls=llm.get("router"),
        llm_answer_calls=llm.get("answer"),
        llm_guard_calls=llm.get("guard"),
        guard=guard,
        classifier=classifier,
        offer=offer,
    )
