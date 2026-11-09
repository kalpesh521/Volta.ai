"""
Routing and safety eval cases for the assistant, stored as JSON Lines in `cases.jsonl`.

One case per line:

    {"id": "...", "question": "...", "intent": "battery",
     "tools": ["get_battery_status"], "analytics": ["calculate_backup_duration"],
     "safety": "answer"}

`safety` is one of:
    answer          a normal grounded answer
    refuse_control  a device-control request that must be refused with no tools
    reject_action   an unsafe or injected control request the action guard must reject

Data lives in a file, not in Python, so cases can be added, reviewed and diffed without
touching code, and so other tooling (CI reports, an experiment tracker) can read them.
`load_cases` validates every row, so a typo fails loudly instead of silently weakening the set.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.modules.assistant.domain.analytics import Analytic
from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.tools.registry import ToolName

DEFAULT_CASES_PATH = Path(__file__).with_name("cases.jsonl")
SAFETY_KINDS = frozenset({"answer", "refuse_control", "reject_action"})
_REQUIRED = frozenset({"id", "question", "intent", "tools", "analytics", "safety"})


class EvalDatasetError(ValueError):
    """A case file is malformed. The message names the line."""


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    intent: str
    tools: frozenset[str]
    analytics: frozenset[str]
    safety: str


def parse_cases(lines: Iterable[str], *, source: str = "<memory>") -> tuple[EvalCase, ...]:
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
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
        if case.id in seen_ids:
            raise EvalDatasetError(f"{where}: duplicate id {case.id!r}")
        if case.question in seen_questions:
            raise EvalDatasetError(f"{where}: duplicate question {case.question!r}")
        seen_ids.add(case.id)
        seen_questions.add(case.question)
        cases.append(case)
    return tuple(cases)


def load_cases(path: Path = DEFAULT_CASES_PATH) -> tuple[EvalCase, ...]:
    with path.open(encoding="utf-8") as handle:
        return parse_cases(handle, source=path.name)


def _build_case(row: object, where: str) -> EvalCase:
    if not isinstance(row, dict):
        raise EvalDatasetError(f"{where}: each line must be a JSON object")
    missing = _REQUIRED - row.keys()
    extra = row.keys() - _REQUIRED
    if missing or extra:
        raise EvalDatasetError(f"{where}: missing {sorted(missing)} unexpected {sorted(extra)}")

    question = row["question"]
    if not isinstance(question, str) or not question.strip():
        raise EvalDatasetError(f"{where}: question must be a non-empty string")
    if row["intent"] not in {intent.value for intent in Intent}:
        raise EvalDatasetError(f"{where}: unknown intent {row['intent']!r}")
    if row["safety"] not in SAFETY_KINDS:
        raise EvalDatasetError(f"{where}: unknown safety {row['safety']!r}")
    tools = _names(row["tools"], {tool.value for tool in ToolName}, "tool", where)
    analytics = _names(row["analytics"], {item.value for item in Analytic}, "analytic", where)
    return EvalCase(
        id=str(row["id"]),
        question=question,
        intent=row["intent"],
        tools=tools,
        analytics=analytics,
        safety=row["safety"],
    )


def _names(value: object, allowed: set[str], kind: str, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EvalDatasetError(f"{where}: {kind}s must be a list of strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvalDatasetError(f"{where}: unknown {kind} {unknown}")
    return frozenset(value)


EVAL_CASES: tuple[EvalCase, ...] = load_cases()
