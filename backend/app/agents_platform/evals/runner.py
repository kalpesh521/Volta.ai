"""
Deterministic trajectory eval: no network, no database, milliseconds per case.

It runs the real supervisor graph (real classification, planning, dispatch, memory, gate) with stub agents
and a scripted model, and checks each turn against its expectation. On top of what a case states, every turn
is checked against the supervisor's structural invariant: the sequence of graph nodes that its route must
produce. A refactor that skips the output gate, calls the model twice, or runs an exclusive agent beside
another cannot pass.

    python -m app.agents_platform.evals                  # print a summary
    python -m app.agents_platform.evals --report out.json

The exit status is non-zero when any case fails, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agents_platform.contracts import AgentName, Plan
from app.agents_platform.evals.cases import (
    DEFAULT_TRAJECTORIES_PATH,
    TrajectoryCase,
    TurnExpectation,
    load_trajectories,
)
from app.agents_platform.evals.stubs import ScriptedLLM, default_stub_agents
from app.agents_platform.orchestration import nodes
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.supervisor import build_supervisor
from app.ai.config import AISettings, LLMRole
from app.modules.assistant.domain.intents import Intent, IntentClassification

_AGENT_NODES = frozenset(agent.value for agent in AgentName)


@dataclass(frozen=True)
class TurnResult:
    question: str
    passed: bool
    failures: tuple[str, ...]
    path: tuple[str, ...]
    agents: tuple[str, ...]
    method: str
    router_calls: int
    answer_calls: int


@dataclass(frozen=True)
class CaseResult:
    id: str
    passed: bool
    turns: tuple[TurnResult, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"turn {i}: {f}" for i, t in enumerate(self.turns, start=1) for f in t.failures)


def expected_path(route: str, agents: Sequence[str]) -> tuple[str, ...]:
    """The graph nodes a route must run, in order. Agents run in one parallel step, shown as `a+b`."""
    head = (nodes.BEGIN_TURN, nodes.CLASSIFY, nodes.PLAN)
    if route == "refusal":
        return (*head, nodes.REFUSE, nodes.FINALIZE)
    step = "+".join(sorted(agents))
    tail = (nodes.JOIN, nodes.SYNTHESIZE, nodes.FINALIZE)
    if route == "exclusive":
        return (*head, step, *tail)  # an action's draft passes straight through: no model, no output gate
    return (*head, step, nodes.JOIN, nodes.SYNTHESIZE, nodes.OUTPUT_GATE, nodes.FINALIZE)


def _collapse(updates: Sequence[str]) -> tuple[str, ...]:
    """Fold the agent nodes of one fan-out into a single step, whatever order they finished in."""
    path: list[str] = []
    fan_out: list[str] = []
    for name in updates:
        if name in _AGENT_NODES:
            fan_out.append(name)
            continue
        if fan_out:
            path.append("+".join(sorted(fan_out)))
            fan_out = []
        path.append(name)
    if fan_out:
        path.append("+".join(sorted(fan_out)))
    return tuple(path)


async def _run_turn(graph, runtime: SupervisorRuntime, llm: ScriptedLLM, thread: str, turn: TurnExpectation):
    routers_before, answers_before = llm.count(LLMRole.ROUTER), llm.count(LLMRole.ANSWER)
    seen: list[str] = []
    final: dict = {}
    async for mode, chunk in graph.astream(
        {"question": turn.question, "request_id": "eval"},
        context=runtime,
        config={"configurable": {"thread_id": thread}},
        stream_mode=["updates", "values"],
    ):
        if mode == "updates":
            seen.extend(chunk)
        else:
            final = chunk
    return (
        final,
        _collapse(seen),
        llm.count(LLMRole.ROUTER) - routers_before,
        llm.count(LLMRole.ANSWER) - answers_before,
    )


def _check(turn: TurnExpectation, final: dict, path: tuple[str, ...], routers: int, answers: int) -> list[str]:
    failures: list[str] = []
    ran = {r["agent"] for r in final.get("results", [])}
    if ran != set(turn.agents):
        failures.append(f"agents: expected {sorted(turn.agents)}, ran {sorted(ran)}")
    route = Plan.model_validate(final.get("plan") or {}).route
    if route != turn.route:
        failures.append(f"route: expected {turn.route}, got {route}")
    if turn.intents is not None and tuple(final.get("intents", ())) != turn.intents:
        failures.append(f"intents: expected {list(turn.intents)}, got {final.get('intents')}")
    if turn.method is not None and final.get("classification_method") != turn.method:
        failures.append(f"method: expected {turn.method}, got {final.get('classification_method')}")
    if turn.llm_router_calls is not None and routers != turn.llm_router_calls:
        failures.append(f"router calls: expected {turn.llm_router_calls}, got {routers}")
    if turn.llm_answer_calls is not None and answers != turn.llm_answer_calls:
        failures.append(f"answer calls: expected {turn.llm_answer_calls}, got {answers}")

    structural = expected_path(turn.route, sorted(turn.agents))
    if path != structural:
        failures.append(f"path: expected {' > '.join(structural)}, got {' > '.join(path)}")
    return failures


async def evaluate_case(case: TrajectoryCase) -> CaseResult:
    graph = build_supervisor(InMemorySaver())
    results: list[TurnResult] = []
    for turn in case.turns:
        classification = IntentClassification(
            primary_intent=Intent(turn.router_intent or Intent.OUT_OF_SCOPE.value)
        )
        llm = ScriptedLLM(classification=classification)
        runtime = SupervisorRuntime(
            agents=default_stub_agents(), llm=llm, settings=AISettings(_env_file=None)
        )
        final, path, routers, answers = await _run_turn(graph, runtime, llm, case.id, turn)
        failures = _check(turn, final, path, routers, answers)
        results.append(
            TurnResult(
                question=turn.question,
                passed=not failures,
                failures=tuple(failures),
                path=path,
                agents=tuple(r["agent"] for r in final.get("results", [])),
                method=final.get("classification_method", ""),
                router_calls=routers,
                answer_calls=answers,
            )
        )
    return CaseResult(case.id, all(t.passed for t in results), tuple(results))


async def evaluate(cases: Sequence[TrajectoryCase]) -> list[CaseResult]:
    return [await evaluate_case(case) for case in cases]


def summarize(results: Sequence[CaseResult]) -> dict[str, object]:
    turns = [t for r in results for t in r.turns]
    return {
        "cases": len(results),
        "cases_passed": sum(r.passed for r in results),
        "turns": len(turns),
        "turns_passed": sum(t.passed for t in turns),
        "rules_or_memory_routed_share": round(
            sum(t.method in ("rules", "memory") for t in turns) / max(len(turns), 1), 3
        ),
        "router_llm_calls": sum(t.router_calls for t in turns),
        "answer_llm_calls": sum(t.answer_calls for t in turns),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agents_platform.evals", description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=DEFAULT_TRAJECTORIES_PATH, help="JSONL file of cases")
    parser.add_argument("--report", type=Path, help="write a JSON report to this path")
    args = parser.parse_args(argv)

    import asyncio

    results = asyncio.run(evaluate(load_trajectories(args.cases)))
    summary = summarize(results)
    failed = [r for r in results if not r.passed]
    for result in failed:
        print(f"FAIL {result.id}")
        for failure in result.failures:
            print(f"     {failure}")
    print(
        f"trajectory evals: {summary['cases_passed']}/{summary['cases']} cases, "
        f"{summary['turns_passed']}/{summary['turns']} turns, "
        f"{summary['rules_or_memory_routed_share']:.0%} routed without the router model"
    )
    if args.report:
        args.report.write_text(
            json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=2), encoding="utf-8"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
