"""Chunking, bill parsing, hybrid rank, and the pgvector filter contract."""
from datetime import date

from app.modules.knowledge.bills import extract_bill_fields
from app.modules.knowledge.chunking import chunk_pages
from app.modules.knowledge.embeddings import HashingEmbedder
from app.modules.knowledge.extract import PageText
from app.modules.knowledge.retrieval import PGVECTOR_SQL, Candidate, cosine, rank_passages


def test_chunks_keep_the_page_number():
    page = "Fault code E04 means the PV voltage is too high. " * 40
    chunks = chunk_pages([PageText(3, page)], size=200, overlap=20)
    assert len(chunks) > 1
    assert all(chunk.page_start == 3 for chunk in chunks)
    assert chunks[0].content_hash != chunks[1].content_hash


def test_bill_fields_are_structured_and_missing_values_stay_empty():
    fields = extract_bill_fields(
        "Units consumed: 320 kWh\nTariff category: LT-I Residential\nSanctioned load: 5 kW\nAmount payable: Rs 2450\n"
    )
    assert fields.units_kwh == 320
    assert fields.tariff_category.startswith("LT-I")
    assert fields.sanctioned_load_kw == 5
    assert fields.amount_inr == 2450
    assert extract_bill_fields("no numbers here").any_found is False


async def test_hashing_embedder_ranks_a_matching_manual_above_noise():
    embedder = HashingEmbedder()
    question = "What does fault code E04 mean on the Growatt inverter?"
    manual = "Growatt inverter fault code E04 means PV voltage is too high. Switch off and call the installer."
    noise = "The refrigerator should stay on during a backup because it is a critical appliance."
    vectors = await embedder.embed_documents([question, manual, noise])
    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])


def test_rank_respects_score_and_cites_the_page():
    embedder_vector = (1.0, 0.0)
    hits = rank_passages(
        [
            Candidate("doc-1", "Growatt manual", "inverter_manual", 4, "fault code E04 PV voltage too high", embedder_vector, brand="Growatt"),
            Candidate("doc-2", "Other", "help", 1, "unrelated gardening advice", (0.0, 1.0)),
        ],
        [1.0, 0.0],
        "fault code E04",
        top_k=2,
        min_score=0.2,
        brand="Growatt",
        today=date(2026, 10, 4),
    )
    assert hits[0].document_id == "doc-1"
    assert hits[0].page == 4
    assert "E04" in hits[0].excerpt


def test_pgvector_query_never_omits_the_user_filter():
    sql = " ".join(PGVECTOR_SQL.split())
    assert "d.owner_user_id = :user_id" in sql
    assert "d.scope = 'shared'" in sql
    assert "d.status = 'published'" in sql
    assert "<=>" in sql
