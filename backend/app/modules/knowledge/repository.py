"""Reads and writes for the knowledge tables. No review rules here."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.knowledge.enums import ReviewStatus, Scope
from app.modules.knowledge.models import (
    BillExtraction,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeReview,
)
from app.modules.knowledge.retrieval import Candidate
from app.modules.onboarding.models import SolarSystem


class KnowledgeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

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
        """Fill the Postgres vector column when it exists. SQLite tests skip this."""
        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql" or not rows:
            return
        from sqlalchemy import text

        for chunk_id, vector in rows:
            literal = "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
            try:
                await self.db.execute(
                    text(
                        "UPDATE knowledge_chunks SET embedding_vec = CAST(:vector AS vector) WHERE id = :id"
                    ),
                    {"vector": literal, "id": chunk_id},
                )
            except Exception:
                return


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
