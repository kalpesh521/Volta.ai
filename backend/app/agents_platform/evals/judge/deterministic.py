"""
Checks code can do exactly, run on every answer before (and independently of) the judge.

Doc 05 §5: deterministic checks first, judge only what code cannot. Numbers that are not in the data and claims of
having switched a device are *hard* failures: the production output gate would have replaced such an answer, so a
prompt that produces them is worse than one that does not no matter how the judge scores its wording.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.agents_platform.evals.judge.rubric import ANSWER_FIELDS
from app.agents_platform.guardrails.output_guard import check_answer
from app.modules.assistant.domain.grounding import check_grounding

# snake_case identifiers: a field name leaking into the wording ("expected_grid_import_kw").
_RAW_FIELD = re.compile(r"\b[a-z]{2,}(?:_[a-z0-9]+)+\b")
_MARKDOWN = re.compile(r"(\*\*|^#{1,6}\s|^\s*[-*]\s|`)", re.MULTILINE)


@dataclass(frozen=True)
class Check:
    name: str
    hard: bool
    detail: str


def run_checks(
    answer: Mapping[str, Any],
    *,
    question: str,
    facts: Mapping[str, Any],
    analytics: Mapping[str, Any],
    freshness: Mapping[str, Any],
) -> list[Check]:
    """The checks an answer fails. Empty means it passed them all."""
    failed: list[Check] = []
    empty = [name for name in ANSWER_FIELDS if not str(answer.get(name, "")).strip()]
    if empty:
        failed.append(Check("empty_field", True, ", ".join(empty)))
    grounding = check_grounding(answer, facts=(facts, analytics, freshness), question=question)
    if not grounding.ok:
        failed.append(Check("ungrounded_numbers", True, ", ".join(grounding.ungrounded)))
    for violation in check_answer(answer):
        failed.append(Check(violation.kind.value, True, violation.rule))
    text = " ".join(str(answer.get(name, "")) for name in ANSWER_FIELDS)
    raw = sorted(set(_RAW_FIELD.findall(text)))
    if raw:
        failed.append(Check("raw_field_name", False, ", ".join(raw)))
    if _MARKDOWN.search(text):
        failed.append(Check("markdown", False, "markdown in plain-text fields"))
    return failed
