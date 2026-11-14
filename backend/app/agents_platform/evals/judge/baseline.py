"""
The release gate (doc 05 §7) over two judge reports.

Hard: a prompt that produces ungrounded numbers or unsafe wording does not ship. Soft: a quality score may not drop
by more than the tolerance against the baseline. Reports are only comparable when they used the same judge prompt,
the same judge model and the same dataset; anything else is refused instead of compared, because the difference
would be the yardstick and not the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_TOLERANCE = 0.02


class IncomparableReportsError(ValueError):
    pass


@dataclass(frozen=True)
class Regression:
    kind: str  # "hard" | "soft"
    what: str


def _require_comparable(current: dict[str, Any], baseline: dict[str, Any]) -> None:
    for key in ("judge_prompt_version", "judge_model", "dataset"):
        if current[key] != baseline[key]:
            raise IncomparableReportsError(
                f"reports differ in {key} ({baseline[key]!r} vs {current[key]!r}); scores are not comparable"
            )


def compare_to_baseline(
    current: dict[str, Any], baseline: dict[str, Any], *, version: str, baseline_version: str, tolerance: float = DEFAULT_TOLERANCE
) -> list[Regression]:
    """Regressions of `version` in `current` against `baseline_version` in `baseline`. Empty means it may ship."""
    _require_comparable(current, baseline)
    if version not in current["summary"] or baseline_version not in baseline["summary"]:
        raise IncomparableReportsError(
            f"version {version!r} or baseline version {baseline_version!r} is not in the reports"
        )
    new, old = current["summary"][version], baseline["summary"][baseline_version]
    found: list[Regression] = []
    if new["hard_failures"] > old["hard_failures"]:
        found.append(Regression("hard", f"hard failures rose from {old['hard_failures']} to {new['hard_failures']}"))
    if new["errors"] > old["errors"]:
        found.append(Regression("soft", f"model errors rose from {old['errors']} to {new['errors']}"))
    for criterion, before in old["mean"].items():
        after = new["mean"].get(criterion)
        if after is None:
            found.append(Regression("soft", f"{criterion} was not scored"))
        elif before > 0 and (before - after) / before > tolerance:
            found.append(Regression("soft", f"{criterion} fell from {before} to {after}"))
    return found
