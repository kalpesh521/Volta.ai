"""
    python -m app.agents_platform.evals.judge cases                                  # validate the dataset (free)
    python -m app.agents_platform.evals.judge run --max-model-calls 20 --versions 2026-10-04.v4 2026-11-13.v6
    python -m app.agents_platform.evals.judge ab --max-model-calls 40 --a 2026-10-04.v4 --b 2026-11-13.v6
    python -m app.agents_platform.evals.judge calibrate --max-model-calls 30

`run`, `ab` and `calibrate` call a real model, so each one **requires `--max-model-calls`** (no default) and prints its
worst-case cost first. `--dry-run` prints the plan and sends nothing. The judge model must be set
(`AI_JUDGE_LLM_MODEL`, `AI_JUDGE_LLM_PROVIDER`); it should be a stronger model than the one it judges, so the run
refuses to use the same model for both unless `--allow-same-model` is given.

Retries and the fallback model are switched off for these runs, so one counted call is one request on the wire.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.agents_platform.evals.budget import ModelCallBudget
from app.agents_platform.evals.judge.baseline import (
    DEFAULT_TOLERANCE,
    IncomparableReportsError,
    compare_to_baseline,
)
from app.agents_platform.evals.judge.calibration import (
    DEFAULT_CALIBRATION_PATH,
    TARGET_SIZE,
    CalibrationReport,
    load_labelled,
    run_calibration,
)
from app.agents_platform.evals.judge.cases import DEFAULT_CASES_PATH, dataset_fingerprint, load_cases
from app.agents_platform.evals.judge.pairwise import CALLS_PER_PAIR, PairReport, run_pairwise
from app.agents_platform.evals.judge.runner import CALLS_PER_CASE, JudgeReport, run_judge
from app.agents_platform.registry import prompts as prompt_registry
from app.ai.config import AISettings, LLMRole, get_ai_settings
from app.ai.llm.factory import ChatModelFactory
from app.ai.llm.gateway import LangChainStructuredLLM
from app.modules.assistant.evals.cases import EvalDatasetError


class JudgeSetupError(RuntimeError):
    """The run cannot start safely. The message says what to set."""


def eval_settings(base: AISettings) -> AISettings:
    """One counted call must be one request: no client retries and no fallback model."""
    return base.model_copy(update={"AI_LLM_MAX_RETRIES": 0, "AI_FALLBACK_LLM_MODEL": ""})


def check_judge_setup(settings: AISettings, *, allow_same_model: bool) -> tuple[str, str]:
    """(answer model, judge model), or a `JudgeSetupError` saying what is wrong."""
    if not settings.AI_JUDGE_LLM_MODEL.strip():
        raise JudgeSetupError("set AI_JUDGE_LLM_MODEL (and AI_JUDGE_LLM_PROVIDER): the judge has no model of its own")
    answer, judge = settings.target(LLMRole.ANSWER), settings.target(LLMRole.JUDGE)
    if answer == judge and not allow_same_model:
        raise JudgeSetupError(
            "the judge and the answer model are the same; a judge should be stronger than what it judges "
            "(use --allow-same-model to run anyway, and treat the scores as optimistic)"
        )
    return f"{answer.provider}:{answer.model}", f"{judge.provider}:{judge.model}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.agents_platform.evals.judge", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    cases = sub.add_parser("cases", help="validate the dataset; sends nothing")
    cases.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)

    def paid(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-model-calls", type=int, required=True, help="hard cap on real model calls for the whole run")
        p.add_argument("--dry-run", action="store_true", help="print the plan and worst-case cost; send nothing")
        p.add_argument("--allow-same-model", action="store_true")
        p.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
        p.add_argument("--limit", type=int, help="use only the first N cases")
        p.add_argument("--report", type=Path, help="write the JSON report here")

    run = sub.add_parser("run", help="score answers per prompt version")
    paid(run)
    run.add_argument("--versions", nargs="+", default=None, help="answer prompt versions; default is the registry default")
    run.add_argument("--baseline", type=Path, help="a previous report; exit 1 if this one regressed")
    run.add_argument("--baseline-version", help="version in the baseline to compare with (default: the first --versions)")
    run.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)

    ab = sub.add_parser("ab", help="pairwise comparison of two prompt versions, position-swapped")
    paid(ab)
    ab.add_argument("--a", required=True)
    ab.add_argument("--b", required=True)

    cal = sub.add_parser("calibrate", help="measure judge agreement with human labels")
    paid(cal)
    cal.add_argument("--labels", type=Path, default=DEFAULT_CALIBRATION_PATH)
    return parser


def _llm(settings: AISettings) -> LangChainStructuredLLM:
    return LangChainStructuredLLM(ChatModelFactory(settings))


def _print_run(report: JudgeReport) -> None:
    print(f"model calls used: {report.calls_used} of {report.max_calls}   judge prompt {report.judge_prompt_version}")
    print(f"answer model {report.answer_model}   judge model {report.judge_model}   dataset {report.dataset}")
    for version in report.versions():
        s = report.summary(version)
        print(f"\n{version}: {s.judged}/{s.cases} judged, {s.errors} errors, {s.hard_failures} hard failures, overall {s.overall}")
        for criterion, mean in s.mean.items():
            print(f"    {criterion:<17} mean {mean:<5}  pass {s.pass_rate[criterion]:.0%}")
    if report.stopped_early:
        print(f"\nSTOPPED EARLY: {report.stopped_early}; {len(report.skipped)} case(s) not run")


def _print_pair(report: PairReport) -> None:
    t = report.tally()
    print(f"model calls used: {report.calls_used} of {report.max_calls}   judge prompt {report.judge_prompt_version}")
    print(f"{report.version_a} (a) vs {report.version_b} (b) on {t['cases']} cases: a wins {t['a_wins']}, b wins {t['b_wins']}, ties {t['ties']}, errors {t['errors']}")
    print(f"hard failures: {t['hard_failures']}")
    if report.stopped_early:
        print(f"STOPPED EARLY: {report.stopped_early}; {len(report.skipped)} case(s) not run")


def _print_calibration(report: CalibrationReport) -> None:
    print(f"model calls used: {report.calls_used} of {report.max_calls}   {report.scored}/{report.labelled} scored")
    for c in report.criteria:
        print(f"    {c.criterion:<17} n={c.n:<3} agreement {c.agreement:.0%}  kappa {c.kappa}  {'TRUSTED' if c.trusted else 'not trusted'}")
    if not report.enough_data:
        print(f"\nonly {report.labelled} labelled answers; the plan asks for {TARGET_SIZE} before a rubric gates anything")
    if report.stopped_early:
        print(f"STOPPED EARLY: {report.stopped_early}")


def _write(path: Path | None, payload: dict) -> None:
    if path:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cases = load_cases(args.cases)
    except (EvalDatasetError, OSError) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return 2
    if args.command == "cases":
        print(f"{len(cases)} cases, dataset {dataset_fingerprint(args.cases)}")
        return 0
    cases = cases[: args.limit] if args.limit else cases

    settings = eval_settings(get_ai_settings())
    try:
        answer_model, judge_model = check_judge_setup(settings, allow_same_model=args.allow_same_model)
        budget = ModelCallBudget(args.max_model_calls)
        dataset = dataset_fingerprint(args.cases)

        if args.command == "run":
            versions = args.versions or [prompt_registry.default_version(prompt_registry.ANSWER)]
            for v in versions:
                prompt_registry.resolve(prompt_registry.ANSWER, v)
            worst = len(cases) * len(versions) * CALLS_PER_CASE
            print(f"plan: {len(cases)} cases x {len(versions)} version(s) x {CALLS_PER_CASE} calls = at most {worst}; cap {budget.maximum}")
            if args.dry_run:
                return 0
            llm = _llm(settings)  # built only after the dry-run exit: a plan never touches a model client
            report = asyncio.run(run_judge(llm, cases, versions, budget, answer_model=answer_model, judge_model=judge_model, dataset=dataset))
            _print_run(report)
            _write(args.report, report.to_dict())
            status = 1 if report.stopped_early else 0
            if args.baseline:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
                base_version = args.baseline_version or versions[0]
                regressions = compare_to_baseline(
                    report.to_dict(), baseline, version=versions[0], baseline_version=base_version, tolerance=args.tolerance
                )
                for r in regressions:
                    print(f"REGRESSION ({r.kind}): {r.what}")
                status = 1 if regressions else status
            return status

        if args.command == "ab":
            for v in (args.a, args.b):
                prompt_registry.resolve(prompt_registry.ANSWER, v)
            print(f"plan: {len(cases)} cases x {CALLS_PER_PAIR} calls = at most {len(cases) * CALLS_PER_PAIR}; cap {budget.maximum}")
            if args.dry_run:
                return 0
            llm = _llm(settings)
            pair = asyncio.run(run_pairwise(llm, cases, args.a, args.b, budget, answer_model=answer_model, judge_model=judge_model, dataset=dataset))
            _print_pair(pair)
            _write(args.report, pair.to_dict())
            return 1 if pair.stopped_early else 0

        labelled = load_labelled(args.labels, {c.id for c in cases})
        print(f"plan: {len(labelled)} labelled answers x 1 call = at most {len(labelled)}; cap {budget.maximum}")
        if args.dry_run:
            return 0
        llm = _llm(settings)
        calibration = asyncio.run(run_calibration(llm, labelled, {c.id: c for c in cases}, budget, judge_model=judge_model))
        _print_calibration(calibration)
        _write(args.report, calibration.to_dict())
        return 1 if calibration.stopped_early else 0
    except (JudgeSetupError, IncomparableReportsError, EvalDatasetError, ValueError) as exc:  # unknown prompt versions are a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
