"""
Re-embed documents that were indexed with a different embedding model.

Retrieval only compares vectors from the same model, so after changing
`AI_EMBEDDING_PROVIDER` / `AI_EMBEDDING_MODEL` every existing document stops matching until it
is re-embedded. Run this once after the change (it is safe to re-run):

    python -m app.modules.knowledge.reembed

Each batch is its own transaction, so an interruption loses at most one batch.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.ai.config import get_ai_settings
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging
from app.modules.knowledge.deps import get_embedder
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.storage import DocumentStorage

logger = logging.getLogger("volta.knowledge.reembed")

_BATCH_DOCUMENTS = 10


async def reembed_all(batch: int = _BATCH_DOCUMENTS) -> int:
    ai = get_ai_settings()
    embedder = get_embedder()
    total = 0
    while True:
        async with AsyncSessionLocal() as session:
            service = KnowledgeService(
                KnowledgeRepository(session),
                embedder,
                DocumentStorage(Path(settings.KNOWLEDGE_STORAGE_DIR)),
                max_bytes=settings.KNOWLEDGE_MAX_UPLOAD_BYTES,
                chunk_chars=ai.RAG_CHUNK_CHARS,
                chunk_overlap=ai.RAG_CHUNK_OVERLAP,
                top_k=ai.RAG_TOP_K,
                min_score=ai.RAG_MIN_SCORE,
            )
            converted = await service.reembed_stale(batch)
            await session.commit()
        if converted == 0:
            return total
        total += converted
        logger.info("re-embedded %d documents so far with %s", total, embedder.model_name)


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    total = asyncio.run(reembed_all())
    logger.info("re-embedding finished: %d documents converted", total)


if __name__ == "__main__":
    main()
