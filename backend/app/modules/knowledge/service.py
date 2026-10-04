"""
Upload, review, and retrieve documents.

Shared manuals and policies stay pending until an admin publishes them.
Private bills and warranties are published for that user only. Deleting a
document deletes its chunks, so the vectors go with it.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.modules.knowledge.bills import extract_bill_fields
from app.modules.knowledge.chunking import chunk_pages, embed_input
from app.modules.knowledge.embeddings import Embedder
from app.modules.knowledge.enums import (
    ADMIN_DOC_TYPES,
    AUTO_SUPERSEDE,
    USER_DOC_TYPES,
    DocType,
    ReviewStatus,
    Scope,
    scope_for,
)
from app.modules.knowledge.errors import (
    DocumentConflictError,
    DocumentTooLargeError,
    KnowledgeNotFoundError,
    KnowledgeRejectedError,
)
from app.modules.knowledge.extract import ALLOWED_SUFFIXES, extract_pages
from app.modules.knowledge.models import BillExtraction, KnowledgeChunk, KnowledgeDocument, KnowledgeReview
from app.modules.knowledge.repository import KnowledgeRepository, bill_payload
from app.modules.knowledge.retrieval import gap_kind, infer_doc_types, rank_passages
from app.modules.knowledge.schemas import BillOut, DocumentOut, ReviewOut, VersionOut
from app.modules.knowledge.storage import DocumentStorage

logger = logging.getLogger("volta.knowledge")

_MAX_CHUNKS = 400
_MEDIA = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


class KnowledgeService:
    def __init__(
        self,
        repo: KnowledgeRepository,
        embedder: Embedder,
        storage: DocumentStorage,
        *,
        max_bytes: int,
        chunk_chars: int,
        chunk_overlap: int,
        top_k: int,
        min_score: float,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._storage = storage
        self._max_bytes = max_bytes
        self._chunk_chars = chunk_chars
        self._chunk_overlap = chunk_overlap
        self._top_k = top_k
        self._min_score = min_score

    async def upload_private(
        self,
        *,
        user_id: uuid.UUID,
        filename: str,
        data: bytes,
        doc_type: str,
        title: str,
        household_id: str | None = None,
        replaces_document_id: uuid.UUID | None = None,
    ) -> DocumentOut:
        parsed = self._parse_type(doc_type, allowed=USER_DOC_TYPES)
        await self._assert_household(user_id, household_id)
        document = await self._ingest(
            user_id=user_id,
            filename=filename,
            data=data,
            doc_type=parsed,
            title=title,
            household_id=household_id,
            brand=None,
            discom=None,
            effective_date=None,
            replaces_document_id=replaces_document_id,
            publish=True,
        )
        return await self._to_out(document, include_history=False)

    async def upload_shared(
        self,
        *,
        admin_id: uuid.UUID,
        filename: str,
        data: bytes,
        doc_type: str,
        title: str,
        brand: str | None = None,
        discom: str | None = None,
        effective_date: date | None = None,
        replaces_document_id: uuid.UUID | None = None,
    ) -> DocumentOut:
        parsed = self._parse_type(doc_type, allowed=ADMIN_DOC_TYPES)
        document = await self._ingest(
            user_id=admin_id,
            filename=filename,
            data=data,
            doc_type=parsed,
            title=title,
            household_id=None,
            brand=_clean(brand),
            discom=_clean(discom),
            effective_date=effective_date,
            replaces_document_id=replaces_document_id,
            publish=True,
        )
        return await self._to_out(document, include_history=False)

    async def list_private(self, user_id: uuid.UUID) -> list[DocumentOut]:
        rows = await self._repo.list_for_user(user_id)
        return [await self._to_out(row, include_history=False) for row in rows]

    async def list_shared(self, status: str | None) -> list[DocumentOut]:
        if status is not None and status not in {item.value for item in ReviewStatus}:
            raise KnowledgeRejectedError("Unknown document status.")
        rows = await self._repo.list_shared(status)
        return [await self._to_out(row, include_history=False) for row in rows]

    async def get_for_user(self, user_id: uuid.UUID, document_id: uuid.UUID) -> DocumentOut:
        document = await self._owned_private(user_id, document_id)
        return await self._to_out(document, include_history=True)

    async def get_shared(self, document_id: uuid.UUID) -> DocumentOut:
        document = await self._shared(document_id)
        return await self._to_out(document, include_history=True)

    def read_file(self, document: KnowledgeDocument) -> tuple[bytes, str, str]:
        return self._storage.read(document.storage_key), document.media_type, document.original_filename

    async def file_for_user(self, user_id: uuid.UUID, document_id: uuid.UUID) -> KnowledgeDocument:
        return await self._owned_private(user_id, document_id)

    async def delete_private(self, user_id: uuid.UUID, document_id: uuid.UUID) -> None:
        document = await self._owned_private(user_id, document_id)
        await self._delete(document)

    async def delete_shared(self, document_id: uuid.UUID) -> None:
        document = await self._shared(document_id)
        await self._delete(document)

    async def review(
        self, *, reviewer_id: uuid.UUID, document_id: uuid.UUID, decision: str, note: str | None
    ) -> DocumentOut:
        document = await self._shared(document_id)
        choice = decision.strip().lower()
        if choice not in {"approve", "reject"}:
            raise KnowledgeRejectedError("Decision must be approve or reject.")
        if document.status != ReviewStatus.PENDING.value:
            raise KnowledgeRejectedError("Only a document waiting for review can be approved or rejected.")
        if choice == "approve":
            await self._publish(document)
        else:
            document.status = ReviewStatus.REJECTED.value
        document.updated_at = datetime.now(timezone.utc)
        document.reviews.append(
            KnowledgeReview(
                document_id=document.id,
                reviewer_id=reviewer_id,
                decision=choice,
                note=_clean(note),
            )
        )
        await self._repo.add_review(document.reviews[-1])
        return await self._to_out(document, include_history=True)

    async def rollback(self, *, reviewer_id: uuid.UUID, document_id: uuid.UUID, note: str | None) -> DocumentOut:
        document = await self._shared(document_id)
        if document.status == ReviewStatus.PENDING.value:
            raise KnowledgeRejectedError("Approve the pending upload instead of rolling back to it.")
        if document.status == ReviewStatus.PUBLISHED.value:
            return await self._to_out(document, include_history=True)
        await self._publish(document)
        document.updated_at = datetime.now(timezone.utc)
        document.reviews.append(
            KnowledgeReview(
                document_id=document.id,
                reviewer_id=reviewer_id,
                decision="rollback",
                note=_clean(note),
            )
        )
        await self._repo.add_review(document.reviews[-1])
        return await self._to_out(document, include_history=True)

    async def retrieve_for_assistant(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        brand: str | None = None,
        discom: str | None = None,
    ) -> dict:
        """Tool payload. Live SOC, solar kW, and grid import are never read here."""
        doc_types = infer_doc_types(question)
        passages = await self._search(user_id, question, doc_types, brand=brand, discom=discom)
        if doc_types and not passages:
            passages = await self._search(user_id, question, None, brand=brand, discom=discom)
        bill = bill_payload(await self._repo.latest_bill(user_id))
        found = bool(passages or bill)
        return {
            "available": found,
            "passages": [passage.__dict__ for passage in passages],
            "bill": bill,
            "gap_kind": None if found else gap_kind(question),
        }

    async def _search(self, user_id, question, doc_types, *, brand, discom):
        candidates = await self._repo.candidates(
            user_id=user_id,
            embedding_model=self._embedder.model_name,
            doc_types=doc_types,
        )
        if not candidates:
            return []
        query = question
        if brand:
            query = f"{query}\n{brand}"
        if discom:
            query = f"{query}\n{discom}"
        vector = await self._embedder.embed_query(query)
        return rank_passages(
            candidates,
            vector,
            question,
            top_k=self._top_k,
            min_score=self._min_score,
            brand=brand,
            discom=discom,
            today=date.today(),
        )

    async def _ingest(
        self,
        *,
        user_id: uuid.UUID,
        filename: str,
        data: bytes,
        doc_type: DocType,
        title: str,
        household_id: str | None,
        brand: str | None,
        discom: str | None,
        effective_date: date | None,
        replaces_document_id: uuid.UUID | None,
        publish: bool,
    ) -> KnowledgeDocument:
        title = _clean(title) or ""
        if len(title) < 2:
            raise KnowledgeRejectedError("Give the document a title.")
        if len(data) == 0:
            raise KnowledgeRejectedError("The file is empty.")
        if len(data) > self._max_bytes:
            raise DocumentTooLargeError(
                f"File is larger than {self._max_bytes // (1024 * 1024)} MB."
            )
        suffix = _suffix(filename)
        if suffix not in ALLOWED_SUFFIXES:
            raise KnowledgeRejectedError("Upload a PDF, TXT, or Markdown file.")
        try:
            pages = extract_pages(filename, data)
        except Exception as exc:
            raise KnowledgeRejectedError("The file could not be read as text.") from exc
        if not pages:
            raise KnowledgeRejectedError("No text could be extracted from this file.")
        chunks = chunk_pages(pages, size=self._chunk_chars, overlap=self._chunk_overlap)
        if not chunks:
            raise KnowledgeRejectedError("No text could be extracted from this file.")
        if len(chunks) > _MAX_CHUNKS:
            raise KnowledgeRejectedError("This document is too long to index. Split it and upload the parts.")

        scope = scope_for(doc_type)
        digest = hashlib.sha256(data).hexdigest()
        duplicate = await self._repo.find_duplicate(
            scope=scope.value, owner_user_id=user_id, doc_type=doc_type.value, sha256=digest
        )
        if duplicate is not None:
            raise DocumentConflictError("This file is already uploaded.")

        replaces = None
        if replaces_document_id is not None:
            replaces = await self._repo.get(replaces_document_id)
            if replaces is None or replaces.doc_type != doc_type.value or replaces.scope != scope.value:
                raise KnowledgeNotFoundError("The document this version replaces was not found.")
            if scope is Scope.PRIVATE and replaces.owner_user_id != user_id:
                raise KnowledgeNotFoundError("The document this version replaces was not found.")

        document_id = uuid.uuid4()
        group_id = replaces.version_group_id if replaces else document_id
        version = replaces.version_number + 1 if replaces else 1
        storage_key = f"{scope.value}/{user_id}/{document_id}{suffix}"
        vectors = await self._embedder.embed_documents(
            [embed_input(title, doc_type.value, chunk.page_start, chunk.text) for chunk in chunks]
        )
        self._storage.save(storage_key, data)
        try:
            document = KnowledgeDocument(
                id=document_id,
                scope=scope.value,
                doc_type=doc_type.value,
                status=ReviewStatus.PUBLISHED.value if publish else ReviewStatus.PENDING.value,
                owner_user_id=user_id,
                household_id=household_id,
                title=title,
                original_filename=Path(filename).name[-255:],
                media_type=_MEDIA[suffix],
                storage_key=storage_key,
                sha256=digest,
                byte_size=len(data),
                brand=brand,
                discom=discom,
                effective_date=effective_date,
                version_group_id=group_id,
                version_number=version,
                embedding_model=self._embedder.model_name,
                page_count=len(pages),
                chunk_count=len(chunks),
            )
            document.chunks = [
                KnowledgeChunk(
                    chunk_index=chunk.index,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                    content=chunk.text,
                    content_hash=chunk.content_hash,
                    embedding_json=_dump_vector(vectors[chunk.index]),
                )
                for chunk in chunks
            ]
            if doc_type is DocType.ELECTRICITY_BILL:
                fields = extract_bill_fields("\n".join(page.text for page in pages))
                if fields.any_found:
                    document.bill = BillExtraction(
                        user_id=user_id,
                        units_kwh=_decimal(fields.units_kwh, "0.001"),
                        tariff_category=fields.tariff_category,
                        sanctioned_load_kw=_decimal(fields.sanctioned_load_kw, "0.001"),
                        amount_inr=_decimal(fields.amount_inr, "0.01"),
                    )
            await self._repo.add(document)
            await self._repo.sync_pgvector(
                [(chunk.id, vectors[chunk.chunk_index]) for chunk in document.chunks]
            )
            if publish and replaces is not None and replaces.status == ReviewStatus.PUBLISHED.value:
                replaces.status = ReviewStatus.SUPERSEDED.value
            if publish and doc_type in AUTO_SUPERSEDE:
                await self._supersede_previous(document)
        except Exception:
            self._storage.delete(storage_key)
            raise
        logger.info(
            "knowledge upload id=%s scope=%s type=%s status=%s chunks=%d user=%s",
            document.id,
            document.scope,
            document.doc_type,
            document.status,
            document.chunk_count,
            user_id,
        )
        return document

    async def _publish(self, document: KnowledgeDocument) -> None:
        document.status = ReviewStatus.PUBLISHED.value
        if DocType(document.doc_type) in AUTO_SUPERSEDE:
            await self._supersede_previous(document)

    async def _supersede_previous(self, document: KnowledgeDocument) -> None:
        previous = await self._repo.published_with_key(
            doc_type=document.doc_type,
            brand=document.brand,
            discom=document.discom,
            exclude_id=document.id,
        )
        for row in previous:
            row.status = ReviewStatus.SUPERSEDED.value

    async def _delete(self, document: KnowledgeDocument) -> None:
        key = document.storage_key
        await self._repo.delete(document)
        self._storage.delete(key)

    async def _assert_household(self, user_id: uuid.UUID, household_id: str | None) -> None:
        if not household_id:
            return
        owner = await self._repo.household_owner(household_id)
        if owner != user_id:
            raise KnowledgeNotFoundError("Home not found.")

    async def _owned_private(self, user_id: uuid.UUID, document_id: uuid.UUID) -> KnowledgeDocument:
        document = await self._repo.get(document_id)
        if (
            document is None
            or document.scope != Scope.PRIVATE.value
            or document.owner_user_id != user_id
        ):
            raise KnowledgeNotFoundError()
        return document

    async def _shared(self, document_id: uuid.UUID) -> KnowledgeDocument:
        document = await self._require(document_id)
        if document.scope != Scope.SHARED.value:
            raise KnowledgeNotFoundError()
        return document

    async def _require(self, document_id: uuid.UUID) -> KnowledgeDocument:
        document = await self._repo.get(document_id)
        if document is None:
            raise KnowledgeNotFoundError()
        return document

    async def _to_out(self, document: KnowledgeDocument, *, include_history: bool) -> DocumentOut:
        bill = None
        bill_row = document.__dict__.get("bill")
        if bill_row is not None:
            payload = bill_payload(bill_row) or {}
            bill = BillOut(
                units_kwh=payload.get("units_kwh"),
                tariff_category=payload.get("tariff_category"),
                sanctioned_load_kw=payload.get("sanctioned_load_kw"),
                amount_inr=payload.get("amount_inr"),
            )
        reviews: list[ReviewOut] = []
        versions: list[VersionOut] = []
        if include_history:
            reviews = [
                ReviewOut(decision=review.decision, note=review.note, created_at=review.created_at)
                for review in sorted(document.__dict__.get("reviews") or [], key=lambda item: item.created_at)
            ]
            for version in await self._repo.versions(document.version_group_id):
                versions.append(
                    VersionOut(
                        id=version.id,
                        version_number=version.version_number,
                        status=version.status,
                        title=version.title,
                        effective_date=version.effective_date,
                        created_at=version.created_at,
                    )
                )
        return DocumentOut(
            id=document.id,
            scope=document.scope,
            doc_type=document.doc_type,
            status=document.status,
            title=document.title,
            brand=document.brand,
            discom=document.discom,
            effective_date=document.effective_date,
            version_group_id=document.version_group_id,
            version_number=document.version_number,
            page_count=document.page_count,
            chunk_count=document.chunk_count,
            byte_size=document.byte_size,
            created_at=document.created_at,
            bill=bill,
            reviews=reviews,
            versions=versions,
        )

    @staticmethod
    def _parse_type(value: str, *, allowed: frozenset[DocType]) -> DocType:
        try:
            parsed = DocType(value.strip())
        except ValueError as exc:
            raise KnowledgeRejectedError("Unknown document type.") from exc
        if parsed not in allowed:
            raise KnowledgeRejectedError("You cannot upload this document type.")
        return parsed


def _suffix(filename: str) -> str:
    name = (filename or "").lower()
    dot = name.rfind(".")
    return name[dot:] if dot >= 0 else ""


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def _decimal(value: float | None, quant: str) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(quant))


def _dump_vector(vector: list[float]) -> str:
    import json

    return json.dumps([round(value, 6) for value in vector])
