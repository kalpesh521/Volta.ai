"""
The proposal state machine, written down once (tracker 4.3).

    proposed ──► awaiting_confirmation ──► confirming ──► queued ──► applied
                      │                        │             │
                      ├─► rejected             ├─► rejected  ├─► failed
                      ├─► cancelled            └─► failed    └─► cancelled
                      └─► expired

A terminal state never changes again. That is what makes the outbox safe to retry: a replayed or duplicate result for a
command that already ended (applied, failed, cancelled) is recognised and ignored instead of flipping the proposal back.
"""
from __future__ import annotations

from collections.abc import Mapping

AWAITING = "awaiting_confirmation"
CONFIRMING = "confirming"
QUEUED = "queued"
APPLIED = "applied"
FAILED = "failed"
CANCELLED = "cancelled"
REJECTED = "rejected"
EXPIRED = "expired"

TRANSITIONS: Mapping[str, frozenset[str]] = {
    AWAITING: frozenset({CONFIRMING, REJECTED, CANCELLED, EXPIRED}),
    CONFIRMING: frozenset({QUEUED, REJECTED, FAILED, AWAITING}),
    QUEUED: frozenset({APPLIED, FAILED, CANCELLED}),
    APPLIED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    REJECTED: frozenset(),
    EXPIRED: frozenset(),
}
TERMINAL = frozenset(state for state, nxt in TRANSITIONS.items() if not nxt)


class IllegalTransitionError(ValueError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"a proposal cannot go from {current!r} to {target!r}")
        self.current, self.target = current, target


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def require_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(current, target)
