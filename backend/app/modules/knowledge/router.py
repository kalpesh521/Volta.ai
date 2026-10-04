"""
Document APIs.

  POST   /knowledge/documents                     logged-in user (bills, warranties)
  GET    /knowledge/documents
  GET    /knowledge/documents/{id}
  GET    /knowledge/documents/{id}/file
  DELETE /knowledge/documents/{id}                also deletes chunks and vectors

  POST   /knowledge/admin/documents               Suryaa admin (manuals, policies, tariffs, help)
  GET    /knowledge/admin/documents
  GET    /knowledge/admin/documents/{id}
  DELETE /knowledge/admin/documents/{id}
  POST   /knowledge/admin/documents/{id}/review
  POST   /knowledge/admin/documents/{id}/rollback

Do not add `from __future__ import annotations` here. slowapi cannot resolve them.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.rate_limit import limiter
from app.models.user import User
from app.modules.knowledge.access import require_suryaa_admin
from app.modules.knowledge.deps import get_knowledge_service
from app.modules.knowledge.enums import AdminDocType, ReviewStatus, UserDocType
from app.modules.knowledge.errors import KnowledgeRejectedError
from app.modules.knowledge.schemas import DocumentOut, ReviewRequest
from app.modules.knowledge.service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=201,
    summary="Upload a private document",
    description="Published immediately for this user only.",
)
@limiter.limit(settings.RATE_LIMIT_KNOWLEDGE)
async def upload_my_document(
    request: Request,
    file: UploadFile = File(..., description="PDF, TXT, or Markdown."),
    doc_type: UserDocType = Form(...),
    title: str = Form(...),
    household_id: str | None = Form(default=None),
    replaces_document_id: str | None = Form(default=None, description="Omit on the first upload."),
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    data = await _read_limited(file)
    return await service.upload_private(
        user_id=current_user.id,
        filename=file.filename or "upload.txt",
        data=data,
        doc_type=doc_type.value,
        title=title,
        household_id=_blank(household_id),
        replaces_document_id=_optional_uuid(replaces_document_id),
    )


@router.get("/documents", response_model=list[DocumentOut], summary="List private documents")
async def list_my_documents(
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> list[DocumentOut]:
    return await service.list_private(current_user.id)


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="Get a private document")
async def get_my_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    return await service.get_for_user(current_user.id, document_id)


@router.get("/documents/{document_id}/file", summary="Download a private document")
async def download_my_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> Response:
    document = await service.file_for_user(current_user.id, document_id)
    data, media, filename = service.read_file(document)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.delete("/documents/{document_id}", status_code=204, summary="Delete a private document")
async def delete_my_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> None:
    await service.delete_private(current_user.id, document_id)


@router.post(
    "/admin/documents",
    response_model=DocumentOut,
    status_code=201,
    summary="Upload a shared document",
    description="Published immediately.",
)
@limiter.limit(settings.RATE_LIMIT_KNOWLEDGE)
async def upload_shared_document(
    request: Request,
    file: UploadFile = File(..., description="PDF, TXT, or Markdown."),
    doc_type: AdminDocType = Form(...),
    title: str = Form(...),
    brand: str | None = Form(default=None),
    discom: str | None = Form(default=None),
    effective_date: str | None = Form(default=None, description="YYYY-MM-DD."),
    replaces_document_id: str | None = Form(default=None, description="Omit on the first upload."),
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    require_suryaa_admin(current_user)
    data = await _read_limited(file)
    return await service.upload_shared(
        admin_id=current_user.id,
        filename=file.filename or "upload.txt",
        data=data,
        doc_type=doc_type.value,
        title=title,
        brand=_blank(brand),
        discom=_blank(discom),
        effective_date=_optional_date(effective_date),
        replaces_document_id=_optional_uuid(replaces_document_id),
    )


@router.get("/admin/documents", response_model=list[DocumentOut], summary="List shared documents")
async def list_shared_documents(
    status: ReviewStatus | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> list[DocumentOut]:
    require_suryaa_admin(current_user)
    return await service.list_shared(status.value if status is not None else None)


@router.get("/admin/documents/{document_id}", response_model=DocumentOut, summary="Get a shared document")
async def get_shared_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    require_suryaa_admin(current_user)
    return await service.get_shared(document_id)


@router.delete("/admin/documents/{document_id}", status_code=204, summary="Delete a shared document")
async def delete_shared_document(
    document_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> None:
    require_suryaa_admin(current_user)
    await service.delete_shared(document_id)


@router.post(
    "/admin/documents/{document_id}/review",
    response_model=DocumentOut,
    summary="Approve or reject a pending document",
)
async def review_shared_document(
    document_id: uuid.UUID,
    payload: ReviewRequest,
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    require_suryaa_admin(current_user)
    return await service.review(
        reviewer_id=current_user.id,
        document_id=document_id,
        decision=payload.decision,
        note=payload.note,
    )


@router.post(
    "/admin/documents/{document_id}/rollback",
    response_model=DocumentOut,
    summary="Restore an older version",
)
async def rollback_shared_document(
    document_id: uuid.UUID,
    note: str | None = Query(default=None, max_length=1000),
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> DocumentOut:
    require_suryaa_admin(current_user)
    return await service.rollback(reviewer_id=current_user.id, document_id=document_id, note=note)


def _blank(value: str | None) -> str | None:
    """Swagger sends "" when "Send empty value" is ticked. Treat that as omitted."""
    if value is None:
        return None
    text = value.strip()
    return text or None


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    text = _blank(value)
    if text is None:
        return None
    try:
        return uuid.UUID(text)
    except ValueError as exc:
        raise KnowledgeRejectedError("replaces_document_id must be a document id.") from exc


def _optional_date(value: str | None) -> date | None:
    text = _blank(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise KnowledgeRejectedError("effective_date must be YYYY-MM-DD, for example 2026-12-04.") from exc


async def _read_limited(file: UploadFile) -> bytes:
    data = await file.read(settings.KNOWLEDGE_MAX_UPLOAD_BYTES + 1)
    await file.close()
    return data
