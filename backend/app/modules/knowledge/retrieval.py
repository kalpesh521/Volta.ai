"""
Metadata-filtered hybrid retrieval.

Candidates are already limited to published shared documents plus the asking
user's private documents. Scoring mixes cosine similarity with lexical overlap,
boosts brand / DISCOM / recency, then applies maximal marginal relevance so
five copies of the same paragraph do not fill the context.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

from app.modules.knowledge.enums import DocType

_TOKEN = re.compile(r"[a-z0-9]+")

# Production path. The user id predicate is not optional.
PGVECTOR_SQL = """
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
  AND d.embedding_model = :embedding_model
  AND (
        d.scope = 'shared'
        OR (d.scope = 'private' AND d.owner_user_id = :user_id)
      )
  AND (:doc_type IS NULL OR d.doc_type = :doc_type)
ORDER BY c.embedding_vec <=> CAST(:query_vector AS vector)
LIMIT :limit
"""


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
) -> list[ScoredPassage]:
    query_tokens = set(_TOKEN.findall(question.lower()))
    scored: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        vector_score = cosine(query_vector, candidate.embedding)
        lexical = _overlap(query_tokens, candidate.content)
        score = (0.75 * vector_score) + (0.25 * lexical)
        score += _metadata_boost(candidate, brand=brand, discom=discom, today=today)
        if score >= min_score:
            scored.append((score, candidate))
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


def _overlap(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = set(_TOKEN.findall(text.lower()))
    return len(query_tokens & text_tokens) / len(query_tokens)


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
