"""
Deterministic, stateless randomness.

Every random draw is a pure function of (seed, keys…) so the same simulated
timestamp produces the same value regardless of tick interval, restart, or
how many households run in one process. That keeps backfills reproducible
and lets two homes in one city share weather while keeping their own habits.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date

_MASK64 = (1 << 64) - 1


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return value ^ (value >> 31)


def stable_hash(text: str) -> int:
    """Process-independent 63-bit hash (Python's hash() is salted for str)."""
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _key_int(part: object) -> int:
    if isinstance(part, bool):
        return int(part)
    if isinstance(part, int):
        return part & _MASK64
    if isinstance(part, float):
        return stable_hash(repr(part))
    if isinstance(part, date):
        return part.toordinal()
    return stable_hash(str(part))


def hash_u64(*parts: object) -> int:
    state = 0
    for part in parts:
        state = _splitmix64(state ^ _key_int(part))
    return state


def uniform(*parts: object) -> float:
    """Uniform in [0, 1)."""
    return (hash_u64(*parts) >> 11) / float(1 << 53)


def uniform_between(low: float, high: float, *parts: object) -> float:
    return low + (high - low) * uniform(*parts)


def normal(*parts: object) -> float:
    """Standard normal via Box–Muller on two hashed uniforms."""
    u1 = max(1e-12, uniform(*parts, "n1"))
    u2 = uniform(*parts, "n2")
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def chance(probability: float, *parts: object) -> bool:
    return uniform(*parts) < probability


def rng_for(*parts: object) -> random.Random:
    return random.Random(hash_u64(*parts))


def smooth_noise(t_seconds: float, period_seconds: float, *parts: object) -> float:
    """
    1-D value noise in [0, 1]: random lattice every `period_seconds`, cosine
    interpolated. Gives autocorrelated jitter instead of per-tick white noise.
    """
    period = max(1e-6, period_seconds)
    position = t_seconds / period
    index = math.floor(position)
    frac = position - index
    v0 = uniform(*parts, index)
    v1 = uniform(*parts, index + 1)
    weight = 0.5 - 0.5 * math.cos(math.pi * frac)
    return v0 + (v1 - v0) * weight


def weighted_choice(weights: dict[str, float], *parts: object) -> str:
    total = sum(max(0.0, w) for w in weights.values())
    if total <= 0:
        return next(iter(weights))
    target = uniform(*parts) * total
    running = 0.0
    for key, weight in weights.items():
        running += max(0.0, weight)
        if target < running:
            return key
    return next(reversed(list(weights)))
