"""FastAPI wiring for the knowledge module."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.config import AISettings, get_ai_settings
from app.core.config import settings
from app.core.database import get_db
from app.modules.knowledge.embeddings import Embedder, build_embedder
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.storage import DocumentStorage


def get_knowledge_settings() -> AISettings:
    return get_ai_settings()


@lru_cache
def get_embedder() -> Embedder:
    return build_embedder(get_ai_settings())


def get_knowledge_service(
    db: AsyncSession = Depends(get_db),
    ai: AISettings = Depends(get_knowledge_settings),
    embedder: Embedder = Depends(get_embedder),
) -> KnowledgeService:
    return KnowledgeService(
        KnowledgeRepository(db),
        embedder,
        DocumentStorage(Path(settings.KNOWLEDGE_STORAGE_DIR)),
        max_bytes=settings.KNOWLEDGE_MAX_UPLOAD_BYTES,
        chunk_chars=ai.RAG_CHUNK_CHARS,
        chunk_overlap=ai.RAG_CHUNK_OVERLAP,
        top_k=ai.RAG_TOP_K,
        min_score=ai.RAG_MIN_SCORE,
    )
