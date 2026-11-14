"""Tiny stats helpers (stdlib has no linear-interpolated quantile on a plain list)."""
from __future__ import annotations

from collections.abc import Sequence


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile, `q` in [0, 1]. Raises ValueError on an empty sequence."""
    if not values:
        raise ValueError("quantile of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
