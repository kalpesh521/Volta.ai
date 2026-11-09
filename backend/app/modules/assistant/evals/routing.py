"""
Deterministic routing eval: no LLM, no network, runs in milliseconds.

For each case it checks that the rule classifier picks the expected intent, that the planner
selects at least the expected tools and analytics, and that control requests get no tools.

    python -m app.modules.assistant.evals.routing                 # print a summary
    python -m app.modules.assistant.evals.routing --report out.json

The exit status is non-zero when any case fails, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from app.modules.assistant.domain.intents import Intent, classify_by_rules
from app.modules.assistant.evals.cases import DEFAULT_CASES_PATH, EvalCase, load_cases
from app.modules.assistant.graph.planner import build_plan


@dataclass(frozen=True)
class CaseResult:
    id: str
    question: str
    passed: bool
    failures: tuple[str, ...]


def evaluate_case(case: EvalCase) -> CaseResult:
    failures: list[str] = []
    match = classify_by_rules(case.question)
    if match.primary.value != case.intent:
        failures.append(f"intent: expected {case.intent}, got {match.primary.value}")

    plan = build_plan(match.intents)
    tools = {tool.value for tool in plan.tools}
    analytics = {item.value for item in plan.analytics}
    if missing := sorted(case.tools - tools):
        failures.append(f"missing tools: {missing}")
    if missing := sorted(case.analytics - analytics):
        failures.append(f"missing analytics: {missing}")

    if case.safety in ("refuse_control", "reject_action"):
        if match.primary is not Intent.DEVICE_CONTROL:
            failures.append("control request was not classified as device_control")
        if plan.tools:
            failures.append(f"control request must select no tools, got {sorted(tools)}")
    if case.safety == "refuse_control" and match.intents != (match.primary,):
        failures.append("control request must not carry secondary intents")

    return CaseResult(case.id, case.question, not failures, tuple(failures))


def evaluate(cases: Sequence[EvalCase]) -> list[CaseResult]:
    return [evaluate_case(case) for case in cases]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the assistant routing evals.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--report", type=Path, help="write a JSON report to this path")
    args = parser.parse_args(argv)

    results = evaluate(load_cases(args.cases))
    failed = [result for result in results if not result.passed]
    for result in failed:
        print(f"FAIL {result.id}: {result.question}")
        for failure in result.failures:
            print(f"     {failure}")
    print(f"routing evals: {len(results) - len(failed)}/{len(results)} passed")

    if args.report:
        args.report.write_text(
            json.dumps(
                {"total": len(results), "failed": len(failed), "results": [asdict(r) for r in results]},
                indent=2,
            ),
            encoding="utf-8",
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
