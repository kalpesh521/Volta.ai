"""
Is the judge fit to be trusted? (doc 05 §5: "calibrate before trusting a rubric".)

A calibration set is answers with human pass/fail labels per criterion. The judge scores them and we measure how
often it agrees with the human, as raw agreement and as Cohen's kappa, which discounts the agreement two raters
would reach by chance. Doc 05 asks for kappa >= 0.7 on 100 labelled answers before a rubric gates anything.

One line per labelled answer in `calibration.jsonl`:

    {"id": "cal-01", "case": "battery-night-discharge", "labelled_by": "author", "answer": {observation..},
     "labels": {"faithful": true, "answers_question": true, ...}}

`labels` need not cover every criterion (an answer can be labelled only where the labeller is sure). `true` means the
answer is good on that criterion. The judge's score >= `PASS_SCORE` counts as a pass.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents_platform.evals.budget import ModelCallBudget
from app.agents_platform.evals.judge.cases import JudgeCase
from app.agents_platform.evals.judge.rubric import (
    ANSWER_FIELDS,
    CRITERIA,
    JUDGE_PROMPT_VERSION,
    PASS_SCORE,
)
from app.agents_platform.evals.judge.runner import MAX_CONSECUTIVE_ERRORS, score_answer
from app.ai.errors import LLMInvocationError, LLMNotConfiguredError
from app.ai.llm.gateway import StructuredLLM
from app.modules.assistant.evals.cases import EvalDatasetError

logger = logging.getLogger("volta.evals.judge")

DEFAULT_CALIBRATION_PATH = Path(__file__).with_name("calibration.jsonl")
TARGET_KAPPA = 0.7
TARGET_SIZE = 100  # labelled answers doc 05 asks for before a rubric gates a release
_CRITERION_NAMES = frozenset(c.value for c in CRITERIA)


@dataclass(frozen=True)
class LabelledAnswer:
    id: str
    case_id: str
    answer: Mapping[str, str]
    labels: Mapping[str, bool]
    labelled_by: str


def parse_labelled(lines: Iterable[str], *, known_cases: set[str], source: str = "calibration") -> list[LabelledAnswer]:
    items: list[LabelledAnswer] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        where = f"{source}:{number}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{where}: invalid JSON ({exc.msg})") from exc
        if not isinstance(raw, dict) or not {"id", "case", "answer", "labels", "labelled_by"} <= raw.keys():
            raise EvalDatasetError(f"{where}: needs id, case, answer, labels, labelled_by")
        if raw["case"] not in known_cases:
            raise EvalDatasetError(f"{where}: unknown case {raw['case']!r}")
        answer, labels = raw["answer"], raw["labels"]
        if not isinstance(answer, dict) or set(answer) != set(ANSWER_FIELDS) or not all(
            isinstance(v, str) and v.strip() for v in answer.values()
        ):
            raise EvalDatasetError(f"{where}: answer must have exactly {list(ANSWER_FIELDS)} as non-empty strings")
        if not isinstance(labels, dict) or not labels or not set(labels) <= _CRITERION_NAMES:
            raise EvalDatasetError(f"{where}: labels must name known criteria, got {sorted(labels) if isinstance(labels, dict) else labels}")
        if not all(isinstance(v, bool) for v in labels.values()):
            raise EvalDatasetError(f"{where}: every label must be true or false")
        items.append(LabelledAnswer(raw["id"], raw["case"], answer, labels, str(raw["labelled_by"])))
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise EvalDatasetError(f"{source}: duplicate ids")
    if not items:
        raise EvalDatasetError(f"{source}: no labelled answers")
    return items


def load_labelled(path: Path, known_cases: set[str]) -> list[LabelledAnswer]:
    return parse_labelled(path.read_text(encoding="utf-8").splitlines(), known_cases=known_cases, source=path.name)


def cohens_kappa(human: Sequence[bool], judge: Sequence[bool]) -> float | None:
    """
    Agreement beyond chance for two binary raters. None when it is undefined: no data, or both raters gave one
    constant answer (chance agreement is then 1 and the statistic divides by zero).
    """
    if len(human) != len(judge):
        raise ValueError("the two raters must label the same items")
    n = len(human)
    if n == 0:
        return None
    observed = sum(h == j for h, j in zip(human, judge)) / n
    p_h, p_j = sum(human) / n, sum(judge) / n
    chance = p_h * p_j + (1 - p_h) * (1 - p_j)
    if chance == 1:
        return None
    return round((observed - chance) / (1 - chance), 3)


@dataclass
class CriterionAgreement:
    criterion: str
    n: int
    agreement: float
    kappa: float | None
    trusted: bool


@dataclass
class CalibrationReport:
    judge_model: str
    judge_prompt_version: str
    calls_used: int = 0
    max_calls: int = 0
    stopped_early: str | None = None
    labelled: int = 0
    scored: int = 0
    criteria: list[CriterionAgreement] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)

    @property
    def enough_data(self) -> bool:
        return self.labelled >= TARGET_SIZE

    def to_dict(self) -> dict[str, Any]:
        return {
            "judge_model": self.judge_model,
            "judge_prompt_version": self.judge_prompt_version,
            "calls_used": self.calls_used,
            "max_calls": self.max_calls,
            "stopped_early": self.stopped_early,
            "labelled": self.labelled,
            "scored": self.scored,
            "target_size": TARGET_SIZE,
            "enough_data": self.enough_data,
            "criteria": [c.__dict__ for c in self.criteria],
            "disagreements": self.disagreements,
        }


def agreement_by_criterion(
    labelled: Sequence[LabelledAnswer], scores: Mapping[str, Mapping[str, int]], *, size: int
) -> tuple[list[CriterionAgreement], list[dict[str, Any]]]:
    """
    `scores[answer id][criterion]` is the judge's 1-5 score. A criterion is only `trusted` when kappa clears
    the target **and** the set is as large as the plan asks, so a kappa from a dozen answers cannot approve a rubric.
    """
    rows: list[CriterionAgreement] = []
    disagreements: list[dict[str, Any]] = []
    for criterion in CRITERIA:
        human, judge = [], []
        for item in labelled:
            if item.id not in scores or criterion.value not in item.labels:
                continue
            human.append(item.labels[criterion.value])
            judge.append(scores[item.id][criterion.value] >= PASS_SCORE)
            if human[-1] != judge[-1]:
                disagreements.append(
                    {"id": item.id, "criterion": criterion.value, "human": human[-1], "judge_score": scores[item.id][criterion.value]}
                )
        kappa = cohens_kappa(human, judge)
        agreement = round(sum(h == j for h, j in zip(human, judge)) / len(human), 3) if human else 0.0
        rows.append(
            CriterionAgreement(
                criterion.value, len(human), agreement, kappa, trusted=kappa is not None and kappa >= TARGET_KAPPA and size >= TARGET_SIZE
            )
        )
    return rows, disagreements


async def run_calibration(
    llm: StructuredLLM,
    labelled: Sequence[LabelledAnswer],
    cases: Mapping[str, JudgeCase],
    budget: ModelCallBudget,
    *,
    judge_model: str,
) -> CalibrationReport:
    """One judge call per labelled answer, each checked against the budget first."""
    report = CalibrationReport(judge_model, JUDGE_PROMPT_VERSION, max_calls=budget.maximum, labelled=len(labelled))
    scores: dict[str, dict[str, int]] = {}
    consecutive_errors = 0
    for item in labelled:
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            report.stopped_early = f"{MAX_CONSECUTIVE_ERRORS} model errors in a row"
            break
        if not budget.can_afford(1):
            report.stopped_early = "model-call budget reached"
            break
        budget.charge(1)
        try:
            verdict, _ = await score_answer(llm, cases[item.case_id], dict(item.answer))
        except (LLMInvocationError, LLMNotConfiguredError) as exc:
            logger.warning("calibration: judge call failed for %s: %s", item.id, exc)
            consecutive_errors += 1
            continue
        consecutive_errors = 0
        scores[item.id] = verdict.scores()
    report.scored = len(scores)
    report.calls_used = budget.used
    report.criteria, report.disagreements = agreement_by_criterion(labelled, scores, size=len(labelled))
    return report
