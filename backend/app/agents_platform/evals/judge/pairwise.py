"""
Pairwise A/B between two prompt versions (doc 05 §5): "which answer is better?" is a more sensitive question than
"score this answer", so it is how a prompt change is decided.

Position bias is real (judges favour the first answer), so every pair is judged twice with the positions swapped. A
version wins a case only if it wins in both orders; any disagreement is a tie. Cost: four model calls per case (two
answers, two verdicts), checked against the budget before the case starts.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.agents_platform.evals.budget import ModelCallBudget
from app.agents_platform.evals.judge.cases import JudgeCase
from app.agents_platform.evals.judge.deterministic import run_checks
from app.agents_platform.evals.judge.rubric import JUDGE_PROMPT_VERSION, PairVerdict, pair_messages
from app.agents_platform.evals.judge.runner import MAX_CONSECUTIVE_ERRORS, generate_answer
from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.gateway import StructuredLLM

logger = logging.getLogger("volta.evals.judge")

CALLS_PER_PAIR = 4
Winner = Literal["a", "b", "tie"]


@dataclass(frozen=True)
class PairOutcome:
    case_id: str
    winner: Winner | None = None
    first_order: str | None = None  # the position the judge picked with `a` first: A, B or tie
    swapped_order: str | None = None  # the same with `b` first
    hard_failures: dict[str, int] = field(default_factory=dict)  # version -> hard failures
    reasons: tuple[str, ...] = ()
    error: str | None = None


def decide(first_order: str, swapped_order: str) -> Winner:
    """`a` wins only when it is picked in both orders. Disagreement means position bias, so it counts as a tie."""
    first = {"A": "a", "B": "b", "tie": "tie"}[first_order]
    swapped = {"A": "b", "B": "a", "tie": "tie"}[swapped_order]
    return first if first == swapped and first != "tie" else "tie"  # type: ignore[return-value]


@dataclass
class PairReport:
    version_a: str
    version_b: str
    answer_model: str
    judge_model: str
    judge_prompt_version: str
    dataset: str
    calls_used: int = 0
    max_calls: int = 0
    stopped_early: str | None = None
    outcomes: list[PairOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def tally(self) -> dict[str, Any]:
        decided = [o for o in self.outcomes if o.winner]
        wins_a = sum(o.winner == "a" for o in decided)
        wins_b = sum(o.winner == "b" for o in decided)
        return {
            "cases": len(decided),
            "a_wins": wins_a,
            "b_wins": wins_b,
            "ties": len(decided) - wins_a - wins_b,
            "errors": sum(1 for o in self.outcomes if o.error),
            "hard_failures": {
                v: sum(o.hard_failures.get(v, 0) for o in decided) for v in (self.version_a, self.version_b)
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_a": self.version_a,
            "version_b": self.version_b,
            "answer_model": self.answer_model,
            "judge_model": self.judge_model,
            "judge_prompt_version": self.judge_prompt_version,
            "dataset": self.dataset,
            "calls_used": self.calls_used,
            "max_calls": self.max_calls,
            "stopped_early": self.stopped_early,
            "skipped": self.skipped,
            "tally": self.tally(),
            "outcomes": [asdict(o) for o in self.outcomes],
        }


async def _judge_pair(llm: StructuredLLM, case: JudgeCase, first: dict, second: dict) -> PairVerdict:
    messages = pair_messages(
        question=case.question,
        facts=case.facts,
        analytics=case.analytics,
        freshness=case.freshness,
        first=first,
        second=second,
    )
    return (await llm.ainvoke(LLMRole.JUDGE, PairVerdict, messages)).output


async def run_pairwise(
    llm: StructuredLLM,
    cases: Sequence[JudgeCase],
    version_a: str,
    version_b: str,
    budget: ModelCallBudget,
    *,
    answer_model: str,
    judge_model: str,
    dataset: str,
) -> PairReport:
    report = PairReport(
        version_a, version_b, answer_model, judge_model, JUDGE_PROMPT_VERSION, dataset, max_calls=budget.maximum
    )
    consecutive_errors = 0
    for case in cases:
        if report.stopped_early:
            report.skipped.append(case.id)
            continue
        if not budget.can_afford(CALLS_PER_PAIR):
            report.stopped_early = "model-call budget reached"
            report.skipped.append(case.id)
            continue
        outcome = await _run_pair(llm, case, version_a, version_b, budget)
        report.outcomes.append(outcome)
        consecutive_errors = consecutive_errors + 1 if outcome.error else 0
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            report.stopped_early = f"{MAX_CONSECUTIVE_ERRORS} model errors in a row"
    report.calls_used = budget.used
    return report


async def _run_pair(
    llm: StructuredLLM, case: JudgeCase, version_a: str, version_b: str, budget: ModelCallBudget
) -> PairOutcome:
    answers: dict[str, dict[str, str]] = {}
    try:
        for version in (version_a, version_b):
            budget.charge(1)
            answers[version], _ = await generate_answer(llm, case, version)
        budget.charge(1)
        first = await _judge_pair(llm, case, answers[version_a], answers[version_b])
        budget.charge(1)
        swapped = await _judge_pair(llm, case, answers[version_b], answers[version_a])
    except (LLMInvocationError, LLMNotConfiguredError) as exc:
        logger.warning("pairwise run: %s failed: %s", case.id, exc)
        return PairOutcome(case.id, error=str(exc))
    hard = {
        v: sum(
            c.hard
            for c in run_checks(
                a, question=case.question, facts=case.facts, analytics=case.analytics, freshness=case.freshness
            )
        )
        for v, a in answers.items()
    }
    return PairOutcome(
        case.id,
        winner=decide(first.better, swapped.better),
        first_order=first.better,
        swapped_order=swapped.better,
        hard_failures=hard,
        reasons=(first.reason, swapped.reason),
    )
