"""
The one thing that stands between an eval run and the user's money: a counter that says no.

Every harness that can reach a real model (the v1/v2 parity run, the L3 judge) takes a `ModelCallBudget`, asks
`can_afford` *before* sending a request and `charge`s what the response really cost. The cap is checked before
spending, not after, and it has no default: a run must be told how much it may spend.
"""
from __future__ import annotations


class ModelCallBudget:
    def __init__(self, maximum: int) -> None:
        if maximum < 0:
            raise ValueError("the model-call budget cannot be negative")
        self.maximum = maximum
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.maximum - self.used

    def can_afford(self, calls: int) -> bool:
        return self.used + calls <= self.maximum

    def charge(self, calls: int) -> None:
        self.used += calls
