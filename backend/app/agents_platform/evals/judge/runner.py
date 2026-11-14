"""
Run the judge: generate an answer per case and prompt version, check it in code, then score it.

**Spending is capped in code.** Each case costs at most two model calls per prompt version (the answer, then the
judge). Before a case starts the runner asks the `ModelCallBudget` whether both calls fit, and it stops, rather than
starting, a case that does not fit. Every call is charged when it is *sent*, whether or not it succeeded, because a
failed request can still be billed. After `MAX_CONSECUTIVE_ERRORS` failures in a row it stops: a wrong key or a
dead endpoint must not be allowed to walk through the whole budget.

Cases run one after another, never concurrently, so the cap cannot be raced and a run is reproducible.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from statistics import fmean
from typing import Any

from app.agents_platform.evals.budget import ModelCallBudget
from app.agents_platform.evals.judge.cases import JudgeCase
from app.agents_platform.evals.judge.deterministic import run_checks
from app.agents_platform.evals.judge.rubric import (
    CRITERIA,
    JUDGE_PROMPT_VERSION,
    PASS_SCORE,
    JudgeVerdict,
    judge_messages,
)
from app.agents_platform.registry.prompts import ResolvedPrompts
from app.ai.config import LLMRole
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.domain.answers import AnswerDraft

logger = logging.getLogger("volta.evals.judge")

CALLS_PER_CASE = 2  # one answer, one verdict
MAX_CONSECUTIVE_ERRORS = 3
_MODEL_ERRORS = (LLMInvocationError, LLMNotConfiguredError)


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    version: str
    answer: dict[str, str] | None = None
    checks: tuple[dict[str, Any], ...] = ()
    scores: dict[str, int] | None = None
    reasons: dict[str, str] | None = None
    error: str | None = None
    tokens: int = 0

    @property
    def hard_failed(self) -> bool:
        return any(c["hard"] for c in self.checks)

    @property
    def judged(self) -> bool:
        return self.scores is not None


@dataclass
class VersionSummary:
    version: str
    cases: int = 0
    judged: int = 0
    errors: int = 0
    hard_failures: int = 0
    mean: dict[str, float] = field(default_factory=dict)
    pass_rate: dict[str, float] = field(default_factory=dict)
    overall: float = 0.0


@dataclass
class JudgeReport:
    answer_model: str
    judge_model: str
    judge_prompt_version: str
    dataset: str
    calls_used: int = 0
    max_calls: int = 0
    stopped_early: str | None = None
    outcomes: list[CaseOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def versions(self) -> list[str]:
        return list(dict.fromkeys(o.version for o in self.outcomes))

    def summary(self, version: str) -> VersionSummary:
        mine = [o for o in self.outcomes if o.version == version]
        judged = [o for o in mine if o.judged]
        summary = VersionSummary(
            version=version,
            cases=len(mine),
            judged=len(judged),
            errors=sum(1 for o in mine if o.error),
            hard_failures=sum(1 for o in mine if o.hard_failed),
        )
        if judged:
            for criterion in CRITERIA:
                values = [o.scores[criterion.value] for o in judged]  # type: ignore[index]
                summary.mean[criterion.value] = round(fmean(values), 3)
                summary.pass_rate[criterion.value] = round(sum(v >= PASS_SCORE for v in values) / len(values), 3)
            summary.overall = round(fmean(summary.mean.values()), 3)
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_model": self.answer_model,
            "judge_model": self.judge_model,
            "judge_prompt_version": self.judge_prompt_version,
            "dataset": self.dataset,
            "calls_used": self.calls_used,
            "max_calls": self.max_calls,
            "stopped_early": self.stopped_early,
            "skipped": self.skipped,
            "summary": {v: asdict(self.summary(v)) for v in self.versions()},
            "outcomes": [asdict(o) for o in self.outcomes],
        }


async def generate_answer(llm: StructuredLLM, case: JudgeCase, version: str) -> tuple[dict[str, str], int]:
    """One answer-model call, built exactly as the synthesizer builds it. Returns the answer and tokens used."""
    messages = ResolvedPrompts(answer_version=version).answer_messages(
        question=case.question,
        intents=list(case.intents),
        facts=dict(case.facts),
        analytics=dict(case.analytics),
        freshness=dict(case.freshness),
    )
    result = await llm.ainvoke(LLMRole.ANSWER, AnswerDraft, messages)
    return result.output.model_dump(), result.usage.total_tokens


async def score_answer(llm: StructuredLLM, case: JudgeCase, answer: dict[str, str]) -> tuple[JudgeVerdict, int]:
    messages = judge_messages(
        question=case.question,
        facts=case.facts,
        analytics=case.analytics,
        freshness=case.freshness,
        answer=answer,
        expect=case.expect,
    )
    result = await llm.ainvoke(LLMRole.JUDGE, JudgeVerdict, messages)
    return result.output, result.usage.total_tokens


async def run_judge(
    llm: StructuredLLM,
    cases: Sequence[JudgeCase],
    versions: Sequence[str],
    budget: ModelCallBudget,
    *,
    answer_model: str,
    judge_model: str,
    dataset: str,
) -> JudgeReport:
    report = JudgeReport(
        answer_model=answer_model,
        judge_model=judge_model,
        judge_prompt_version=JUDGE_PROMPT_VERSION,
        dataset=dataset,
        max_calls=budget.maximum,
    )
    consecutive_errors = 0
    for version in versions:
        for case in cases:
            label = f"{version}:{case.id}"
            if report.stopped_early:
                report.skipped.append(label)
                continue
            if not budget.can_afford(CALLS_PER_CASE):
                report.stopped_early = "model-call budget reached"
                report.skipped.append(label)
                continue

            outcome = await _run_case(llm, case, version, budget)
            report.outcomes.append(outcome)
            consecutive_errors = consecutive_errors + 1 if outcome.error else 0
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                report.stopped_early = f"{MAX_CONSECUTIVE_ERRORS} model errors in a row"
    report.calls_used = budget.used
    return report


async def _run_case(llm: StructuredLLM, case: JudgeCase, version: str, budget: ModelCallBudget) -> CaseOutcome:
    tokens = 0
    budget.charge(1)
    try:
        answer, used = await generate_answer(llm, case, version)
    except _MODEL_ERRORS as exc:
        logger.warning("judge run: answer call failed for %s:%s: %s", version, case.id, exc)
        return CaseOutcome(case.id, version, error=f"answer: {exc}")
    tokens += used
    checks = tuple(
        asdict(c)
        for c in run_checks(
            answer, question=case.question, facts=case.facts, analytics=case.analytics, freshness=case.freshness
        )
    )
    budget.charge(1)
    try:
        verdict, used = await score_answer(llm, case, answer)
    except _MODEL_ERRORS as exc:
        logger.warning("judge run: judge call failed for %s:%s: %s", version, case.id, exc)
        return CaseOutcome(case.id, version, answer=answer, checks=checks, error=f"judge: {exc}", tokens=tokens)
    return CaseOutcome(
        case.id,
        version,
        answer=answer,
        checks=checks,
        scores=verdict.scores(),
        reasons={c.value: getattr(verdict, c.value).reason for c in CRITERIA},
        tokens=tokens + used,
    )
