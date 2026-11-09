"""
Indexed retrieval against a real Postgres (pgvector + full-text). Opt-in:

    TEST_POSTGRES_URL=postgresql+asyncpg://volta:volta@localhost:5434/volta_auth pytest tests/test_knowledge_postgres.py

The database must already be migrated (`alembic upgrade head`). Every test runs inside a
transaction that is rolled back, so nothing is left behind.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.user import User
from app.modules.knowledge.embeddings import HashingEmbedder
from app.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.storage import DocumentStorage

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set")

MODEL = "hashing-256"
EMBEDDER = HashingEmbedder(256, MODEL)
SCAN_LIMIT = 400  # KnowledgeRepository.candidates default


@asynccontextmanager
async def _session():
    """A session inside a transaction that is always rolled back."""
    engine = create_async_engine(POSTGRES_URL)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


def with_db(test):
    """
    Give a test a database session opened on the test's own event loop.

    asyncpg connections are tied to the loop that created them, and an async *fixture* may be
    created on a different loop than the test (it depends on what ran earlier in the session).
    Opening the connection inside the test removes that dependency.
    """

    @functools.wraps(test)
    async def wrapper() -> None:
        async with _session() as db:
            await test(db)

    wrapper.__signature__ = inspect.Signature()  # hide `db` from pytest's fixture lookup
    return wrapper


async def _user(db: AsyncSession) -> uuid.UUID:
    user = User(email=f"{uuid.uuid4().hex}@example.com", name="Retrieval Tester", hashed_password=None)
    db.add(user)
    await db.flush()
    return user.id


async def _document(
    db: AsyncSession,
    chunks: list[str],
    *,
    scope: str = "shared",
    owner: uuid.UUID | None = None,
    status: str = "published",
    model: str = MODEL,
    age_days: int = 0,
    doc_type: str = "inverter_manual",
) -> KnowledgeDocument:
    group = uuid.uuid4()
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    document = KnowledgeDocument(
        id=group,
        scope=scope,
        doc_type=doc_type,
        status=status,
        owner_user_id=owner,
        title=f"Doc {group.hex[:6]}",
        original_filename="doc.txt",
        media_type="text/plain",
        storage_key=f"test/{group}.txt",
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        byte_size=1,
        version_group_id=group,
        version_number=1,
        embedding_model=model,
        page_count=1,
        chunk_count=len(chunks),
        created_at=created,
    )
    vectors = await EMBEDDER.embed_documents(chunks)
    document.chunks = [
        KnowledgeChunk(
            chunk_index=index,
            page_start=1,
            page_end=1,
            content=content,
            content_hash=uuid.uuid4().hex,
            embedding_json=json.dumps(vector),
        )
        for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]
    db.add(document)
    await db.flush()
    await KnowledgeRepository(db).sync_pgvector(
        [(chunk.id, vectors[chunk.chunk_index]) for chunk in document.chunks]
    )
    return document


async def _search(db: AsyncSession, question: str, user_id: uuid.UUID, **overrides):
    vector = await EMBEDDER.embed_query(question)
    arguments = {
        "user_id": user_id,
        "embedding_model": MODEL,
        "doc_types": None,
        "query_vector": vector,
        "question": question,
        "vector_limit": 40,
        "lexical_limit": 40,
    }
    return await KnowledgeRepository(db).search_candidates(**{**arguments, **overrides})


def _noise(count: int) -> list[str]:
    return [f"Appendix {i}: maintenance schedule {i}, torque values and cable lengths for unit {i}." for i in range(count)]


@with_db
async def test_an_exact_fault_code_is_found_among_many_chunks(db):
    user = await _user(db)
    await _document(db, ["Fault code E04 means the PV input voltage is too high."] + _noise(80))

    found = await _search(db, "what does fault code E04 mean", user)

    assert any("E04" in candidate.content for candidate in found)
    assert len(found) <= 80  # bounded by the two indexed searches, not the corpus size


@with_db
async def test_old_chunks_are_not_lost_when_the_corpus_outgrows_the_scan_limit(db):
    """The previous path scanned the newest 400 chunks, whatever the question was."""
    user = await _user(db)
    await _document(db, ["Warranty claim number WQ-7731 must be quoted when calling support."], age_days=30)
    await _document(db, _noise(SCAN_LIMIT + 50))
    repo = KnowledgeRepository(db)

    scanned = await repo.candidates(user_id=user, embedding_model=MODEL, doc_types=None)
    indexed = await _search(db, "warranty claim number WQ-7731", user)

    assert not any("WQ-7731" in c.content for c in scanned)  # what the old path returned
    assert any("WQ-7731" in c.content for c in indexed)


@with_db
async def test_private_documents_are_visible_only_to_their_owner(db):
    owner, other = await _user(db), await _user(db)
    await _document(db, ["My private note: the spare inverter key is in the garage."], scope="private", owner=owner)

    assert any("garage" in c.content for c in await _search(db, "where is the spare inverter key garage", owner))
    assert await _search(db, "where is the spare inverter key garage", other) == []


@with_db
async def test_unpublished_and_other_model_documents_are_excluded(db):
    user = await _user(db)
    await _document(db, ["Pending review: battery refresh procedure."], status="pending")
    await _document(db, ["Old model: battery refresh procedure."], model="some-other-model")

    assert await _search(db, "battery refresh procedure", user) == []


@with_db
async def test_the_document_type_filter_applies_to_both_searches(db):
    user = await _user(db)
    await _document(db, ["Net metering policy: export credit is settled monthly."], doc_type="net_metering_policy")
    await _document(db, ["Inverter note: export credit display on the screen."], doc_type="inverter_manual")

    found = await _search(db, "export credit", user, doc_types=("net_metering_policy",))

    assert found and all(c.doc_type == "net_metering_policy" for c in found)


@with_db
async def test_a_failing_indexed_search_falls_back_to_the_scan_and_keeps_the_transaction_usable(db):
    user = await _user(db)
    await _document(db, ["Fault code E04 means the PV input voltage is too high."])

    # A query vector of the wrong size makes pgvector raise inside the savepoint.
    found = await _search(db, "fault code E04", user, query_vector=[0.1] * 8)

    assert any("E04" in c.content for c in found)
    assert (await db.execute(text("SELECT 1"))).scalar_one() == 1


@with_db
async def test_reembedding_converts_documents_and_fills_the_vector_column(db):
    user = await _user(db)
    stale = await _document(db, ["Stale document about fault code E04."], model="legacy-model", owner=user, scope="private")
    service = KnowledgeService(
        KnowledgeRepository(db),
        EMBEDDER,
        DocumentStorage(Path("/tmp")),
        max_bytes=1,
        chunk_chars=900,
        chunk_overlap=120,
        top_k=5,
        min_score=0.0,
    )
    assert await _search(db, "fault code E04", user) == []  # invisible under the current model

    assert await service.reembed_stale(limit=10) >= 1

    await db.refresh(stale)
    assert stale.embedding_model == MODEL
    assert any("E04" in c.content for c in await _search(db, "fault code E04", user))
    filled = (
        await db.execute(
            text("SELECT count(*) FROM knowledge_chunks WHERE document_id = :d AND embedding_vec IS NOT NULL"),
            {"d": stale.id},
        )
    ).scalar_one()
    assert filled == 1
