"""
v1 / v2 parity on a live deployment, with a hard cap on real model calls.

    export VOLTA_TOKEN=<access token>            # never passed on the command line
    python -m app.agents_platform.evals.parity --base-url http://127.0.0.1:8000 --max-model-calls 3

The fake-model tests prove v2 routes and structures like v1. Only a real model shows whether the *answers* hold
up, so this asks the same question on `/assistant/me/ask` and `/v2/assistant/me/ask` and compares them.

**Cost control is enforced here, not promised.** Before each request the harness works out the most it can cost,
and it refuses to send one it cannot afford:

* a question the offline rules route confidently costs one call (the answer);
* any other question may also reach the router model, so it is counted as two;
* after a response, the cost actually incurred is read back from its own metadata and charged.

`--max-model-calls` is required and has no default, so a run can never spend by accident. A run that would exceed it
stops early and says so.

What is compared, per question: routing (intents, tools, analytics), confidence, data freshness, whether the model
was used, and the sources. Those must match. The numbers each answer states and the wording are shown but never fail
the run: two model calls differ in phrasing even at a low temperature, and judging wording is the job of the
L3 judge in doc 05, which does not exist yet. So this is evidence that v2 is not *worse on the facts*, not a verdict
on tone.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.modules.assistant.domain.intents import classify_by_rules

V1_PATH = "/assistant/me/ask"
V2_PATH = "/v2/assistant/me/ask"
DEFAULT_QUESTIONS = ("Why is the battery discharging?",)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")

Ask = Callable[[str, str], Awaitable[dict[str, Any]]]

# Fields where two correct answers legitimately differ. Both answers already passed the numeric grounding gate, so a
# number one states and the other does not is a different choice of which (verified) fact to mention, not an error.
INFORMATIONAL = frozenset({"numbers stated"})


class ModelCallBudget:
    """A counter that says no. The cap is the user's money, so it is checked before spending, not after."""

    def __init__(self, maximum: int) -> None:
        if maximum < 0:
            raise ValueError("the model-call budget cannot be negative")
        self.maximum = maximum
        self.used = 0

    def can_afford(self, calls: int) -> bool:
        return self.used + calls <= self.maximum

    def charge(self, calls: int) -> None:
        self.used += calls


def worst_case_calls(question: str) -> int:
    """Most a request can cost: the answer, plus the router model when the rules are not sure."""
    return 1 if classify_by_rules(question).confident else 2


def calls_incurred(response: dict[str, Any]) -> int:
    """What a request really cost, from its own metadata."""
    meta = response.get("meta", {})
    return int(bool(meta.get("llm_used"))) + int(meta.get("classification_method") == "llm")


@dataclass(frozen=True)
class Difference:
    field: str
    v1: Any
    v2: Any
    # Informational differences are shown but never fail the run (see `INFORMATIONAL`).
    informational: bool = False


@dataclass
class QuestionReport:
    question: str
    differences: list[Difference] = field(default_factory=list)
    v1_answer: dict[str, str] = field(default_factory=dict)
    v2_answer: dict[str, str] = field(default_factory=dict)
    wording_overlap: float = 0.0

    @property
    def matches(self) -> bool:
        return not any(not d.informational for d in self.differences)


@dataclass
class ParityReport:
    questions: list[QuestionReport] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    calls_used: int = 0
    max_calls: int = 0

    @property
    def passed(self) -> bool:
        return bool(self.questions) and all(q.matches for q in self.questions)


def numbers_in(answer: dict[str, str]) -> frozenset[str]:
    """Every number an answer states, as written, ignoring the stamped data time."""
    text = " ".join(v for k, v in answer.items() if k != "data_time")
    return frozenset(m.replace(",", "") for m in _NUMBER.findall(text))


def _words(answer: dict[str, str]) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z]+", " ".join(v for k, v in answer.items() if k != "data_time").lower()))


def wording_overlap(left: dict[str, str], right: dict[str, str]) -> float:
    a, b = _words(left), _words(right)
    return round(len(a & b) / len(a | b), 3) if a | b else 1.0


def compare(question: str, v1: dict[str, Any], v2: dict[str, Any]) -> QuestionReport:
    """Facts must match; wording is only measured."""
    report = QuestionReport(
        question=question,
        v1_answer=v1["answer"],
        v2_answer=v2["answer"],
        wording_overlap=wording_overlap(v1["answer"], v2["answer"]),
    )
    checks: list[tuple[str, Any, Any]] = [
        ("intents", v1["intents"], v2["intents"]),
        ("tools_used", sorted(v1["tools_used"]), sorted(v2["tools_used"])),
        ("analytics_used", v1["analytics_used"], v2["analytics_used"]),
        ("confidence", v1["confidence"], v2["confidence"]),
        ("data_freshness.status", v1["data_freshness"]["status"], v2["data_freshness"]["status"]),
        ("meta.llm_used", v1["meta"]["llm_used"], v2["meta"]["llm_used"]),
        ("meta.fallback_reason", v1["meta"]["fallback_reason"], v2["meta"]["fallback_reason"]),
        ("sources", [s["document_id"] for s in v1.get("sources", [])], [s["document_id"] for s in v2.get("sources", [])]),
        ("numbers stated", sorted(numbers_in(v1["answer"])), sorted(numbers_in(v2["answer"]))),
    ]
    report.differences = [Difference(name, a, b, name in INFORMATIONAL) for name, a, b in checks if a != b]
    return report


async def run_parity(
    ask_v1: Ask, ask_v2: Ask, questions: Sequence[str], budget: ModelCallBudget
) -> ParityReport:
    report = ParityReport(max_calls=budget.maximum)
    for question in questions:
        worst = worst_case_calls(question)
        if not budget.can_afford(2 * worst):  # both endpoints must be affordable, or the pair is useless
            report.skipped.append(question)
            continue
        first = await ask_v1(V1_PATH, question)
        budget.charge(calls_incurred(first))
        second = await ask_v2(V2_PATH, question)
        budget.charge(calls_incurred(second))
        report.questions.append(compare(question, first, second))
    report.calls_used = budget.used
    return report


def _client_ask(client: httpx.AsyncClient, headers: dict[str, str]) -> Ask:
    async def ask(path: str, question: str) -> dict[str, Any]:
        response = await client.post(path, json={"question": question}, headers=headers)
        response.raise_for_status()
        return response.json()

    return ask


def render(report: ParityReport) -> str:
    lines = [f"model calls used: {report.calls_used} of {report.max_calls}"]
    for q in report.questions:
        lines.append(f"\n{'MATCH' if q.matches else 'DIFFERS'}  {q.question!r}  (wording overlap {q.wording_overlap:.0%})")
        for d in q.differences:
            lines.append(f"    {d.field}{' (informational)' if d.informational else ''}: v1={d.v1!r}  v2={d.v2!r}")
        for label, answer in (("v1", q.v1_answer), ("v2", q.v2_answer)):
            lines.append(f"    {label} observation: {answer['observation']}")
            lines.append(f"    {label} explanation: {answer['explanation']}")
    for question in report.skipped:
        lines.append(f"\nSKIPPED (would exceed the call budget): {question!r}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.agents_platform.evals.parity", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--max-model-calls", type=int, required=True, help="hard cap on real model calls for the whole run")
    parser.add_argument("--question", action="append", help="repeatable; default is one battery question")
    parser.add_argument("--token-env", default="VOLTA_TOKEN", help="environment variable holding the access token")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"set {args.token_env} to an access token first", file=sys.stderr)
        return 2

    async def go() -> ParityReport:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=60.0) as client:
            ask = _client_ask(client, {"Authorization": f"Bearer {token}"})
            return await run_parity(ask, ask, args.question or DEFAULT_QUESTIONS, ModelCallBudget(args.max_model_calls))

    report = asyncio.run(go())
    print(render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
