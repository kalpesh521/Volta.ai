"""
Judge cases: frozen inputs for answer-quality evals, one JSON object per line in `judge_cases.jsonl`.

A case is exactly what the answer prompt shows the model (`facts`, `analytics`, `freshness`), captured once from the
real pipeline over a fixed telemetry snapshot (see `tests/capture_judge_fixtures.py`). Because the data is frozen, the
numbers a good answer must use never change between runs, so a score moves only when the prompt, the model or the
judge moves.

    {"id": "battery-night-discharge", "tags": ["battery"], "question": "Why is the battery discharging?",
     "intents": ["battery"], "facts": {...}, "analytics": {...}, "freshness": {...},
     "expect": ["States the battery is discharging and its charge level"]}

`expect` are reviewer notes the judge sees: plain statements of what a good answer covers. Optional.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.modules.assistant.domain.intents import Intent
from app.modules.assistant.evals.cases import EvalDatasetError

DEFAULT_CASES_PATH = Path(__file__).with_name("judge_cases.jsonl")
_KEYS = frozenset({"id", "tags", "question", "intents", "facts", "analytics", "freshness", "expect"})
_REQUIRED = frozenset({"id", "question", "intents", "facts", "analytics", "freshness"})
_INTENTS = frozenset(i.value for i in Intent)


@dataclass(frozen=True)
class JudgeCase:
    id: str
    question: str
    intents: tuple[str, ...]
    facts: Mapping[str, Any]
    analytics: Mapping[str, Any]
    freshness: Mapping[str, Any]
    tags: tuple[str, ...] = ()
    expect: tuple[str, ...] = ()


def parse_case(raw: object, *, where: str = "case") -> JudgeCase:
    if not isinstance(raw, dict):
        raise EvalDatasetError(f"{where}: expected an object")
    missing, unknown = _REQUIRED - raw.keys(), raw.keys() - _KEYS
    if missing or unknown:
        raise EvalDatasetError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    if not isinstance(raw["id"], str) or not raw["id"].strip():
        raise EvalDatasetError(f"{where}: id must be a non-empty string")
    where = f"case {raw['id']!r}"
    if not isinstance(raw["question"], str) or not raw["question"].strip():
        raise EvalDatasetError(f"{where}: question must be a non-empty string")
    intents = raw["intents"]
    if not isinstance(intents, list) or not intents or not set(intents) <= _INTENTS:
        raise EvalDatasetError(f"{where}: intents must be a non-empty list of known intents")
    for key in ("facts", "analytics", "freshness"):
        if not isinstance(raw[key], dict):
            raise EvalDatasetError(f"{where}: {key} must be an object")
    if not raw["facts"]:
        raise EvalDatasetError(f"{where}: facts is empty, so there is nothing to judge an answer against")
    for key in ("tags", "expect"):
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise EvalDatasetError(f"{where}: {key} must be a list of non-empty strings")
    return JudgeCase(
        id=raw["id"],
        question=raw["question"],
        intents=tuple(intents),
        facts=raw["facts"],
        analytics=raw["analytics"],
        freshness=raw["freshness"],
        tags=tuple(raw.get("tags", ())),
        expect=tuple(raw.get("expect", ())),
    )


def parse_cases(lines: Iterable[str], *, source: str = "cases") -> list[JudgeCase]:
    cases: list[JudgeCase] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            cases.append(parse_case(json.loads(line), where=f"{source}:{number}"))
        except json.JSONDecodeError as exc:
            raise EvalDatasetError(f"{source}:{number}: invalid JSON ({exc.msg})") from exc
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise EvalDatasetError(f"{source}: duplicate case ids {duplicates}")
    if not cases:
        raise EvalDatasetError(f"{source}: no cases")
    return cases


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[JudgeCase]:
    return parse_cases(path.read_text(encoding="utf-8").splitlines(), source=path.name)


def dataset_fingerprint(path: Path) -> str:
    """Short hash of the file, pinned in every report so two runs on different data are never compared."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
