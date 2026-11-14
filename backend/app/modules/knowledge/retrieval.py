"""
Metadata-filtered hybrid retrieval.

On Postgres, candidates come from two indexed searches whose results are united:

  * nearest neighbours by embedding (pgvector HNSW), for meaning, and
  * full-text matches (GIN on a tsvector), for exact words such as fault codes.

Both are limited to published shared documents plus the asking user's private
documents. The union is then scored in Python: cosine similarity mixed with
BM25, fused by rank (RRF), with boosts for brand / DISCOM / recency, and maximal marginal
relevance so five copies of the same paragraph do not fill the context. Candidates are ordered by Reciprocal Rank
Fusion of a cosine ranking and a BM25 ranking (see `ranking.py`).
Without Postgres (tests, SQLite) the repository scans a bounded set instead.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from app.modules.knowledge.enums import DocType
from app.modules.knowledge.ranking import bm25_scores, query_terms, ranks, reciprocal_rank_fusion

_TOKEN = re.compile(r"[a-z0-9]+")

# Both queries share one visibility filter. The user id predicate is not optional.
_CANDIDATE_SELECT = """
SELECT
    c.id AS chunk_id,
    c.content AS content,
    c.page_start AS page_start,
    c.embedding_json AS embedding_json,
    d.id AS document_id,
    d.title AS title,
    d.doc_type AS doc_type,
    d.brand AS brand,
    d.discom AS discom,
    d.effective_date AS effective_date
FROM knowledge_chunks AS c
JOIN knowledge_documents AS d ON d.id = c.document_id
WHERE d.status = 'published'
  AND c.quarantined = false
  AND d.embedding_model = :embedding_model
  AND (
        d.scope = 'shared'
        OR (d.scope = 'private' AND d.owner_user_id = :user_id)
      )
"""
_DOC_TYPE_FILTER = "  AND d.doc_type IN :doc_types\n"


def vector_candidates_sql(*, filter_doc_types: bool) -> str:
    """Nearest chunks by cosine distance. Chunks without a stored vector are skipped."""
    return (
        _CANDIDATE_SELECT
        + "  AND c.embedding_vec IS NOT NULL\n"
        + (_DOC_TYPE_FILTER if filter_doc_types else "")
        + "ORDER BY c.embedding_vec <=> CAST(:query_vector AS vector)\nLIMIT :limit"
    )


def lexical_candidates_sql(*, filter_doc_types: bool) -> str:
    """Chunks containing any query word, best full-text rank first."""
    return (
        _CANDIDATE_SELECT
        + "  AND c.content_tsv @@ to_tsquery('simple', :tsquery)\n"
        + (_DOC_TYPE_FILTER if filter_doc_types else "")
        + "ORDER BY ts_rank_cd(c.content_tsv, to_tsquery('simple', :tsquery), 32) DESC\nLIMIT :limit"
    )


_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from has have how i in is it its me my of on or "
    "our should so that the their there this to was we what when where which who why will with would you your".split()
)
_MAX_QUERY_TERMS = 24


def build_tsquery(question: str) -> str:
    """
    OR-query of the meaningful words in a question, for `to_tsquery('simple', ...)`.

    Words are kept to `[a-z0-9]+`, so the result can never contain tsquery syntax.
    Returns an empty string when nothing meaningful is left.
    """
    terms = dict.fromkeys(
        token for token in _TOKEN.findall(question.lower()) if len(token) >= 2 and token not in _STOPWORDS
    )
    return " | ".join(list(terms)[:_MAX_QUERY_TERMS])


@dataclass(frozen=True)
class Candidate:
    document_id: str
    title: str
    doc_type: str
    page: int | None
    content: str
    embedding: tuple[float, ...]
    brand: str | None = None
    discom: str | None = None
    effective_date: date | None = None


@dataclass(frozen=True)
class ScoredPassage:
    document_id: str
    title: str
    doc_type: str
    page: int | None
    excerpt: str
    score: float


def cosine(left: tuple[float, ...] | list[float], right: tuple[float, ...] | list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def infer_doc_types(question: str) -> tuple[str, ...] | None:
    text = question.lower()
    found: list[str] = []
    if re.search(r"\b(fault|error)\s*-?\s*codes?\b", text) or "inverter manual" in text:
        found.append(DocType.INVERTER_MANUAL.value)
    if "battery manual" in text or "battery fault" in text:
        found.append(DocType.BATTERY_MANUAL.value)
    if "net metering" in text or "net-metering" in text or "discom policy" in text:
        found.append(DocType.NET_METERING_POLICY.value)
    if re.search(r"\btariff\s+(order|document|schedule|circular)\b", text) or "merc" in text:
        found.append(DocType.TARIFF.value)
    if "electricity bill" in text or "my bill" in text:
        found.append(DocType.ELECTRICITY_BILL.value)
    if "warranty" in text or "installation document" in text or "installation certificate" in text:
        found.append(DocType.INSTALLATION_WARRANTY.value)
    if "help" in text and re.search(r"\b(doc|article|guide|documentation)\b", text):
        found.append(DocType.HELP.value)
    unique = tuple(dict.fromkeys(found))
    return unique or None


def gap_kind(question: str) -> str:
    if re.search(r"\b(fault|error)\s*-?\s*codes?\b", question.lower()):
        return "fault_code"
    return "missing"


def rank_passages(
    candidates: list[Candidate],
    query_vector: list[float],
    question: str,
    *,
    top_k: int,
    min_score: float,
    brand: str | None = None,
    discom: str | None = None,
    today: date | None = None,
    rrf_k: int = 60,
) -> list[ScoredPassage]:
    """
    The best `top_k` passages. A candidate must clear a relevance floor (`min_score`, on cosine and BM25 as before) to
    be considered at all; the survivors are ordered by Reciprocal Rank Fusion of their cosine and BM25 rankings plus
    the brand / DISCOM / recency boosts, then thinned by maximal marginal relevance.
    """
    if not candidates:
        return []
    vector_scores = [cosine(query_vector, c.embedding) for c in candidates]
    lexical = bm25_scores(query_terms(question), [c.content for c in candidates])
    fused = reciprocal_rank_fusion(ranks(vector_scores), ranks(lexical), k=rrf_k)

    scored: list[tuple[float, Candidate]] = []
    for candidate, vector_score, lexical_score, fused_score in zip(candidates, vector_scores, lexical, fused):
        boost = _metadata_boost(candidate, brand=brand, discom=discom, today=today)
        if (0.75 * vector_score) + (0.25 * lexical_score) + boost >= min_score:
            scored.append((fused_score + boost, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = _mmr(scored[: max(top_k * 4, top_k)], top_k)
    return [
        ScoredPassage(
            document_id=candidate.document_id,
            title=candidate.title,
            doc_type=candidate.doc_type,
            page=candidate.page,
            excerpt=_excerpt(candidate.content),
            score=round(score, 4),
        )
        for score, candidate in chosen
    ]


def _metadata_boost(
    candidate: Candidate, *, brand: str | None, discom: str | None, today: date | None
) -> float:
    boost = 0.0
    if brand and candidate.brand and candidate.brand.lower() == brand.lower():
        boost += 0.05
    if discom and candidate.discom and candidate.discom.lower() == discom.lower():
        boost += 0.05
    if today and candidate.effective_date and candidate.effective_date <= today:
        age_days = (today - candidate.effective_date).days
        boost += 0.03 if age_days < 400 else 0.0
    return boost


def _mmr(scored: list[tuple[float, Candidate]], top_k: int, diversity: float = 0.7) -> list[tuple[float, Candidate]]:
    pool = list(scored)
    selected: list[tuple[float, Candidate]] = []
    while pool and len(selected) < top_k:
        if not selected:
            best = max(pool, key=lambda item: item[0])
        else:

            def mmr_score(item: tuple[float, Candidate]) -> float:
                similarity = max(cosine(item[1].embedding, pick[1].embedding) for pick in selected)
                return (diversity * item[0]) - ((1 - diversity) * similarity)

            best = max(pool, key=mmr_score)
        selected.append(best)
        pool.remove(best)
    return selected


def _excerpt(text: str, limit: int = 480) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
