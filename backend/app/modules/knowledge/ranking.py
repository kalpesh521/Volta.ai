"""
Passage ranking: BM25 for words, cosine for meaning, fused by Reciprocal Rank Fusion (tracker 3.9).

The earlier score was `0.75 x cosine + 0.25 x (share of question words present)`. Two problems: the weights mixed two
numbers that live on different scales (a cosine of 0.3 is a good match, a word share of 0.3 is a poor one), and a
word that appears in every chunk counted as much as a rare one such as `E04`.

* **BM25** weights a word by how rare it is among the candidates (IDF) and by how often it appears in a chunk
  relative to the chunk's length, so `E04` outweighs "inverter".
* **Reciprocal Rank Fusion** combines two *rankings*, not two scores: a chunk scores `1/(k + rank)` in each list and
  the two are added. It needs no calibration between cosine and BM25, so changing the embedding model cannot silently
  change how much words count. `k = 60` is the value from the original paper; it is a setting.

Pure functions, no I/O.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from has have how i in is it its me my of on or "
    "our should so that the their there this to was we what when where which who why will with would you your".split()
)
K1 = 1.5  # how quickly repeating a word stops adding score
B = 0.75  # how strongly a long chunk is penalised


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def query_terms(question: str) -> list[str]:
    """The meaningful words of a question, in order, without repeats."""
    return list(dict.fromkeys(t for t in tokens(question) if t not in _STOPWORDS))


def bm25_scores(terms: Sequence[str], documents: Sequence[str]) -> list[float]:
    """
    BM25 of every document against `terms`, scaled so the best document is 1.0 (all 0.0 when nothing matches).

    IDF is computed over `documents` themselves (the candidate set), which is what is available at ranking time.
    """
    if not terms or not documents:
        return [0.0] * len(documents)
    counts = [Counter(tokens(d)) for d in documents]
    lengths = [sum(c.values()) for c in counts]
    average = (sum(lengths) / len(lengths)) or 1.0
    n = len(documents)
    scores: list[float] = []
    for count, length in zip(counts, lengths):
        total = 0.0
        for term in terms:
            frequency = count.get(term, 0)
            if frequency == 0:
                continue
            containing = sum(1 for c in counts if term in c)
            idf = math.log(1 + (n - containing + 0.5) / (containing + 0.5))
            total += idf * frequency * (K1 + 1) / (frequency + K1 * (1 - B + B * length / average))
        scores.append(total)
    best = max(scores)
    return [s / best for s in scores] if best > 0 else [0.0] * n


def ranks(scores: Sequence[float]) -> list[int | None]:
    """1-based rank of each score, best first. A score of zero or less has no rank (it did not match)."""
    order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: scores[i], reverse=True)
    result: list[int | None] = [None] * len(scores)
    for position, index in enumerate(order, start=1):
        result[index] = position
    return result


def reciprocal_rank_fusion(*rankings: Sequence[int | None], k: int = 60) -> list[float]:
    """
    Fused score of each item over several rankings, scaled to (0, 1] where 1.0 is first in every list.
    An item missing from a list (rank None) simply earns nothing from it.
    """
    if not rankings:
        return []
    best_possible = len(rankings) / (k + 1)
    count = len(rankings[0])
    return [
        sum(1 / (k + r[i]) for r in rankings if r[i] is not None) / best_possible for i in range(count)
    ]
