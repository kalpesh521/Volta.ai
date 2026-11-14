"""Reads and writes for the knowledge tables. No review rules here."""
from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import and_, bindparam, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.modules.knowledge.enums import ReviewStatus, Scope
from app.modules.knowledge.models import (
    BillExtraction,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeReview,
)
from app.modules.knowledge.retrieval import (
    Candidate,
    build_tsquery,
    lexical_candidates_sql,
    vector_candidates_sql,
)
from app.modules.onboarding.models import SolarSystem

logger = logging.getLogger("volta.knowledge.repository")


class KnowledgeRepository:
    def __init__(
        self, db: AsyncSession, *, hnsw_ef_search: int = 100, hnsw_iterative_scan: str = "relaxed_order"
    ) -> None:
        self.db = db
        self._hnsw_ef_search = hnsw_ef_search
        self._hnsw_iterative_scan = hnsw_iterative_scan

    async def add(self, document: KnowledgeDocument) -> KnowledgeDocument:
        self.db.add(document)
        await self.db.flush()
        return document

    async def get(self, document_id: uuid.UUID) -> KnowledgeDocument | None:
        result = await self.db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .options(
                selectinload(KnowledgeDocument.bill),
                selectinload(KnowledgeDocument.reviews),
            )
        )
        return result.scalar_one_or_none()

    async def find_duplicate(
        self, *, scope: str, owner_user_id: uuid.UUID | None, doc_type: str, sha256: str
    ) -> KnowledgeDocument | None:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.scope == scope,
            KnowledgeDocument.doc_type == doc_type,
            KnowledgeDocument.sha256 == sha256,
            KnowledgeDocument.status != ReviewStatus.REJECTED.value,
        )
        if scope == Scope.PRIVATE.value:
            stmt = stmt.where(KnowledgeDocument.owner_user_id == owner_user_id)
        result = await self.db.execute(stmt.limit(1))
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id: uuid.UUID) -> list[KnowledgeDocument]:
        result = await self.db.execute(
            select(KnowledgeDocument)
            .where(
                KnowledgeDocument.scope == Scope.PRIVATE.value,
                KnowledgeDocument.owner_user_id == user_id,
            )
            .options(selectinload(KnowledgeDocument.bill))
            .order_by(KnowledgeDocument.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_shared(self, status: str | None = None) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(KnowledgeDocument.scope == Scope.SHARED.value)
        if status:
            stmt = stmt.where(KnowledgeDocument.status == status)
        result = await self.db.execute(
            stmt.options(selectinload(KnowledgeDocument.reviews)).order_by(KnowledgeDocument.created_at.desc())
        )
        return list(result.scalars().all())

    async def versions(self, version_group_id: uuid.UUID) -> list[KnowledgeDocument]:
        result = await self.db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.version_group_id == version_group_id)
            .order_by(KnowledgeDocument.version_number.asc())
        )
        return list(result.scalars().all())

    async def published_with_key(
        self, *, doc_type: str, brand: str | None, discom: str | None, exclude_id: uuid.UUID
    ) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.scope == Scope.SHARED.value,
            KnowledgeDocument.status == ReviewStatus.PUBLISHED.value,
            KnowledgeDocument.doc_type == doc_type,
            KnowledgeDocument.id != exclude_id,
        )
        if brand:
            stmt = stmt.where(func.lower(KnowledgeDocument.brand) == brand.lower())
        else:
            stmt = stmt.where(KnowledgeDocument.brand.is_(None))
        if discom:
            stmt = stmt.where(func.lower(KnowledgeDocument.discom) == discom.lower())
        else:
            stmt = stmt.where(KnowledgeDocument.discom.is_(None))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def add_review(self, review: KnowledgeReview) -> None:
        self.db.add(review)
        await self.db.flush()

    async def delete(self, document: KnowledgeDocument) -> None:
        await self.db.delete(document)
        await self.db.flush()

    async def household_owner(self, household_id: str) -> uuid.UUID | None:
        result = await self.db.execute(
            select(SolarSystem.user_id).where(SolarSystem.household_id == household_id)
        )
        return result.scalar_one_or_none()

    async def latest_bill(self, user_id: uuid.UUID) -> BillExtraction | None:
        result = await self.db.execute(
            select(BillExtraction)
            .join(KnowledgeDocument, KnowledgeDocument.id == BillExtraction.document_id)
            .where(
                BillExtraction.user_id == user_id,
                KnowledgeDocument.status == ReviewStatus.PUBLISHED.value,
            )
            .order_by(BillExtraction.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def search_candidates(
        self,
        *,
        user_id: uuid.UUID,
        embedding_model: str,
        doc_types: tuple[str, ...] | None,
        query_vector: list[float],
        question: str,
        vector_limit: int,
        lexical_limit: int,
    ) -> list[Candidate]:
        """
        Candidates for ranking. On Postgres: nearest neighbours united with full-text matches.
        Anywhere else, or if those indexed searches return nothing or fail, a bounded scan.
        """
        if self._uses_postgres_indexes():
            indexed = await self._indexed_candidates(
                user_id=user_id,
                embedding_model=embedding_model,
                doc_types=doc_types,
                query_vector=query_vector,
                tsquery=build_tsquery(question),
                vector_limit=vector_limit,
                lexical_limit=lexical_limit,
            )
            if indexed:
                return indexed
        return await self.candidates(user_id=user_id, embedding_model=embedding_model, doc_types=doc_types)

    def _uses_postgres_indexes(self) -> bool:
        return settings.KNOWLEDGE_USE_PGVECTOR and self.db.get_bind().dialect.name == "postgresql"

    async def _indexed_candidates(
        self,
        *,
        user_id: uuid.UUID,
        embedding_model: str,
        doc_types: tuple[str, ...] | None,
        query_vector: list[float],
        tsquery: str,
        vector_limit: int,
        lexical_limit: int,
    ) -> list[Candidate]:
        params: dict[str, object] = {"user_id": user_id, "embedding_model": embedding_model}
        if doc_types:
            params["doc_types"] = list(doc_types)
        searches = [
            (
                vector_candidates_sql(filter_doc_types=bool(doc_types)),
                {**params, "query_vector": _vector_literal(query_vector), "limit": vector_limit},
            )
        ]
        if tsquery:
            searches.append(
                (
                    lexical_candidates_sql(filter_doc_types=bool(doc_types)),
                    {**params, "tsquery": tsquery, "limit": lexical_limit},
                )
            )

        await self._tune_hnsw()
        found: dict[str, Candidate] = {}
        for sql, bound in searches:
            statement = text(sql)
            if doc_types:
                statement = statement.bindparams(bindparam("doc_types", expanding=True))
            try:
                # A savepoint keeps one failing search (bad dimensions, missing column) from
                # aborting the request's transaction.
                async with self.db.begin_nested():
                    rows = (await self.db.execute(statement, bound)).mappings().all()
            except SQLAlchemyError:
                logger.warning("indexed knowledge search failed; falling back to a scan", exc_info=True)
                return []
            for row in rows:
                found.setdefault(str(row["chunk_id"]), _candidate_from_row(row))
        return list(found.values())

    async def _tune_hnsw(self) -> None:
        """
        Per-transaction HNSW settings. `iterative_scan` makes a filtered nearest-neighbour search keep looking until
        it has enough rows, instead of returning the few neighbours that happened to pass the visibility filter.
        Each setting has its own savepoint: a pgvector too old to know one must not fail the search or the others.
        """
        settings_to_apply = [("hnsw.ef_search", str(self._hnsw_ef_search))]
        if self._hnsw_iterative_scan != "off":
            settings_to_apply.append(("hnsw.iterative_scan", self._hnsw_iterative_scan))
        for name, value in settings_to_apply:
            try:
                async with self.db.begin_nested():
                    await self.db.execute(text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value})
            except SQLAlchemyError:
                logger.info("pgvector does not support %s; continuing without it", name)

    async def candidates(
        self,
        *,
        user_id: uuid.UUID,
        embedding_model: str,
        doc_types: tuple[str, ...] | None,
        limit: int = 400,
    ) -> list[Candidate]:
        stmt = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(
                KnowledgeDocument.status == ReviewStatus.PUBLISHED.value,
                KnowledgeChunk.quarantined.is_(False),  # instruction-like text is stored for review, never retrieved
                KnowledgeDocument.embedding_model == embedding_model,
                or_(
                    KnowledgeDocument.scope == Scope.SHARED.value,
                    and_(
                        KnowledgeDocument.scope == Scope.PRIVATE.value,
                        KnowledgeDocument.owner_user_id == user_id,
                    ),
                ),
            )
            .order_by(KnowledgeDocument.created_at.desc(), KnowledgeChunk.chunk_index.asc())
            .limit(limit)
        )
        if doc_types:
            stmt = stmt.where(KnowledgeDocument.doc_type.in_(doc_types))
        result = await self.db.execute(stmt)
        candidates: list[Candidate] = []
        for chunk, document in result.all():
            candidates.append(
                Candidate(
                    document_id=str(document.id),
                    title=document.title,
                    doc_type=document.doc_type,
                    page=chunk.page_start,
                    content=chunk.content,
                    embedding=tuple(json.loads(chunk.embedding_json)),
                    brand=document.brand,
                    discom=document.discom,
                    effective_date=document.effective_date,
                )
            )
        return candidates

    async def sync_pgvector(self, rows: list[tuple[uuid.UUID, list[float]]]) -> None:
        """Fill the Postgres vector column used by the ANN index. SQLite tests skip this."""
        if not rows or self.db.get_bind().dialect.name != "postgresql":
            return
        try:
            async with self.db.begin_nested():
                for chunk_id, vector in rows:
                    await self.db.execute(
                        text("UPDATE knowledge_chunks SET embedding_vec = CAST(:vector AS vector) WHERE id = :id"),
                        {"vector": _vector_literal(vector), "id": chunk_id},
                    )
        except SQLAlchemyError:
            # The JSON column still holds every embedding, so retrieval keeps working.
            logger.warning("could not store pgvector embeddings for %d chunks", len(rows), exc_info=True)

    async def documents_with_other_embedding_model(self, embedding_model: str, limit: int) -> list[KnowledgeDocument]:
        """Documents embedded with a different model than the one now configured."""
        result = await self.db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.embedding_model != embedding_model)
            .order_by(KnowledgeDocument.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def chunks_of(self, document_id: uuid.UUID) -> list[KnowledgeChunk]:
        result = await self.db.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document_id)
            .order_by(KnowledgeChunk.chunk_index)
        )
        return list(result.scalars().all())


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def _candidate_from_row(row) -> Candidate:
    return Candidate(
        document_id=str(row["document_id"]),
        title=row["title"],
        doc_type=row["doc_type"],
        page=row["page_start"],
        content=row["content"],
        embedding=tuple(json.loads(row["embedding_json"])),
        brand=row["brand"],
        discom=row["discom"],
        effective_date=row["effective_date"],
    )


def bill_payload(bill: BillExtraction | None) -> dict | None:
    if bill is None:
        return None
    return {
        "units_kwh": float(bill.units_kwh) if bill.units_kwh is not None else None,
        "tariff_category": bill.tariff_category,
        "sanctioned_load_kw": float(bill.sanctioned_load_kw) if bill.sanctioned_load_kw is not None else None,
        "amount_inr": float(bill.amount_inr) if bill.amount_inr is not None else None,
        "document_id": str(bill.document_id),
        "as_of": bill.created_at.date().isoformat() if bill.created_at else None,
    }
