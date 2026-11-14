"""Tracker 3.9: BM25 and Reciprocal Rank Fusion, the iterative-scan settings and the vector width tool's guards."""
from __future__ import annotations

import math
from datetime import date

import pytest

from app.modules.knowledge.ranking import (
    bm25_scores,
    query_terms,
    ranks,
    reciprocal_rank_fusion,
)
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.retrieval import (
    Candidate,
    lexical_candidates_sql,
    rank_passages,
    vector_candidates_sql,
)
from app.modules.knowledge.vector_width import MAX_HNSW_DIMENSIONS, VectorWidthError, resize_vector_column


# --- BM25 -----------------------------------------------------------------------------------------


def test_query_terms_drop_stopwords_and_repeats_and_keep_order():
    assert query_terms("What does the fault code E04 mean on my inverter? E04!") == ["fault", "code", "e04", "mean", "inverter"]


def test_a_rare_word_outweighs_a_common_one():
    documents = [
        "inverter inverter inverter inverter manual overview",
        "inverter fault code E04 means PV voltage too high",
        "inverter installation notes and mounting",
        "inverter warranty terms",
    ]
    scores = bm25_scores(["inverter", "e04"], documents)
    assert scores.index(max(scores)) == 1, "E04 appears once but only here, so it must beat four mentions of 'inverter'"


def test_scores_are_scaled_so_the_best_document_is_one_and_a_miss_is_zero():
    scores = bm25_scores(["battery"], ["battery charge", "solar output", "battery battery temperature limit"])
    assert max(scores) == 1.0 and scores[1] == 0.0 and 0 < min(scores[0], scores[2]) <= 1.0


def test_repeating_a_word_helps_less_and_less():
    docs = ["alpha", "alpha alpha", "alpha alpha alpha alpha alpha alpha alpha alpha", "beta gamma delta epsilon"]
    one, two, many, _ = bm25_scores(["alpha"], docs)
    assert one < two < many and (two - one) > (many - two) / 6  # diminishing returns


def test_a_longer_document_with_the_same_hit_scores_lower():
    short = "fault e04"
    long = "fault e04 " + "filler " * 80
    s_short, s_long = bm25_scores(["e04"], [short, long])
    assert s_short > s_long


def test_nothing_to_match_scores_zero_without_dividing_by_zero():
    assert bm25_scores([], ["a b"]) == [0.0]
    assert bm25_scores(["x"], []) == []
    assert bm25_scores(["zzz"], ["a b", "c d"]) == [0.0, 0.0]


def test_the_idf_matches_the_formula_for_a_two_document_case():
    # n=2, "a" in 1 doc: idf = ln(1 + 1.5/1.5) = ln 2; both docs same length, so avg=len
    docs = ["a b", "c d"]
    first, second = bm25_scores(["a"], docs)
    expected = math.log(2) * 1 * 2.5 / (1 + 1.5)
    assert second == 0.0 and first == 1.0
    raw = math.log(2) * 1 * (1.5 + 1) / (1 + 1.5 * (1 - 0.75 + 0.75 * 1))
    assert raw == pytest.approx(expected)


# --- ranks and fusion -----------------------------------------------------------------------------


def test_ranks_are_one_based_best_first_and_a_zero_has_none():
    assert ranks([0.2, 0.9, 0.0, 0.5]) == [3, 1, None, 2]


def test_fusion_is_one_for_first_in_every_list_and_lower_otherwise():
    fused = reciprocal_rank_fusion([1, 2, 3], [1, 3, 2], k=60)
    assert fused[0] == pytest.approx(1.0) and fused[0] > fused[1] > 0 and fused[0] > fused[2]


def test_an_item_missing_from_a_list_earns_nothing_from_it():
    fused = reciprocal_rank_fusion([1, 2], [None, 1], k=60)
    assert fused[0] == pytest.approx(0.5), "first in one list of two earns half of the best possible"
    assert fused[1] == pytest.approx((1 / 62 + 1 / 61) / (2 / 61)), "second in one list plus first in the other"


def test_being_in_both_lists_beats_topping_one():
    fused = reciprocal_rank_fusion([1, 2, None], [None, 2, 1], k=60)
    assert fused[1] > fused[0] and fused[1] > fused[2]


def test_a_larger_k_flattens_the_difference_between_ranks():
    sharp = reciprocal_rank_fusion([1, 5], k=1)
    flat = reciprocal_rank_fusion([1, 5], k=1000)
    assert (sharp[0] - sharp[1]) > (flat[0] - flat[1])


def test_no_rankings_fuse_to_nothing():
    assert reciprocal_rank_fusion() == []


# --- rank_passages --------------------------------------------------------------------------------


def candidate(doc: str, text: str, vector: tuple[float, float], **kw) -> Candidate:
    return Candidate(doc, f"title {doc}", "inverter_manual", 1, text, vector, **kw)


def rank(cands, vector=(1.0, 0.0), question="fault code E04", **kw):
    return rank_passages(cands, list(vector), question, top_k=kw.pop("top_k", 3), min_score=kw.pop("min_score", 0.05), **kw)


def test_the_word_match_wins_when_meaning_is_a_tie():
    hits = rank(
        [
            candidate("generic", "inverter installation overview", (0.7, 0.7)),
            candidate("exact", "fault code E04 PV voltage too high", (0.7, 0.7)),
        ]
    )
    assert hits[0].document_id == "exact"


def test_the_meaning_match_wins_when_the_words_are_a_tie():
    hits = rank(
        [
            candidate("far", "fault code E04 appears here", (0.0, 1.0)),
            candidate("near", "fault code E04 appears here", (1.0, 0.0)),
        ]
    )
    assert hits[0].document_id == "near"


def test_a_candidate_below_the_relevance_floor_is_never_returned():
    hits = rank([candidate("noise", "gardening advice", (0.0, 1.0))], min_score=0.2)
    assert hits == []
    assert rank([]) == []


def test_the_best_candidate_comes_first_and_every_score_stays_in_range():
    # MMR may reorder the rest to avoid near-duplicates, so only the first place is pinned.
    cands = [candidate(f"d{i}", f"fault code E04 note {i}", (1.0 - i * 0.2, i * 0.2)) for i in range(5)]
    hits = rank(cands, top_k=5)
    assert hits[0].document_id == "d0" and hits[0].score == max(h.score for h in hits)
    assert all(0 < h.score <= 1.2 for h in hits)


def test_the_brand_and_recency_boosts_still_apply():
    plain = candidate("plain", "fault code E04", (1.0, 0.0))
    branded = candidate("branded", "fault code E04", (1.0, 0.0), brand="Growatt", effective_date=date(2026, 6, 1))
    hits = rank([plain, branded], brand="Growatt", today=date(2026, 11, 1))
    assert hits[0].document_id == "branded"


def test_rrf_k_changes_how_far_the_second_place_trails_the_first():
    cands = [candidate("a", "fault code E04", (1.0, 0.0)), candidate("b", "fault", (0.6, 0.8))]
    tight = {h.document_id: h.score for h in rank(cands, rrf_k=1, top_k=2)}
    loose = {h.document_id: h.score for h in rank(cands, rrf_k=1000, top_k=2)}
    assert tight["a"] == loose["a"] == 1.0
    assert tight["b"] < loose["b"] < 1.0


# --- the SQL and the repository settings ----------------------------------------------------------


@pytest.mark.parametrize("build", [vector_candidates_sql, lexical_candidates_sql])
def test_both_indexed_queries_leave_quarantined_chunks_out(build):
    assert "c.quarantined = false" in " ".join(build(filter_doc_types=False).split())


async def test_the_hnsw_settings_never_fail_a_search_when_pgvector_does_not_know_them():
    from sqlalchemy.exc import SQLAlchemyError

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeDb:
        def __init__(self, unknown: set[str]) -> None:
            self.unknown, self.applied = unknown, []

        def begin_nested(self):
            return Savepoint()

        async def execute(self, statement, params):
            if params["name"] in self.unknown:
                raise SQLAlchemyError("unrecognized configuration parameter")
            self.applied.append((params["name"], params["value"]))

    old = FakeDb({"hnsw.iterative_scan"})
    await KnowledgeRepository(old, hnsw_ef_search=64)._tune_hnsw()
    assert old.applied == [("hnsw.ef_search", "64")], "an unknown setting is skipped; the others still apply"

    new = FakeDb(set())
    await KnowledgeRepository(new, hnsw_iterative_scan="strict_order")._tune_hnsw()
    assert new.applied == [("hnsw.ef_search", "100"), ("hnsw.iterative_scan", "strict_order")]

    off = FakeDb(set())
    await KnowledgeRepository(off, hnsw_iterative_scan="off")._tune_hnsw()
    assert off.applied == [("hnsw.ef_search", "100")]


# --- the vector width guards ----------------------------------------------------------------------


async def test_a_width_pgvector_cannot_index_or_that_makes_no_sense_is_refused_before_touching_the_database():
    class Untouched:
        async def execute(self, *a, **k):
            raise AssertionError("the database must not be reached")

    for bad in (0, -5, MAX_HNSW_DIMENSIONS + 1, 3072):
        with pytest.raises(VectorWidthError):
            await resize_vector_column(Untouched(), bad)
