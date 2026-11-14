"""
Re-embed documents that were indexed with a different embedding model.

Retrieval only compares vectors from the same model, so after changing
`AI_EMBEDDING_PROVIDER` / `AI_EMBEDDING_MODEL` every existing document stops matching until it
is re-embedded. Run this once after the change (it is safe to re-run):

    python -m app.modules.knowledge.reembed

To also resize the pgvector column to the new model's width first (needed when the dimensions change):

    python -m app.modules.knowledge.reembed --resize-vector

Each batch is its own transaction, so an interruption loses at most one batch.
"""
from __future__ import annotations

import argparse
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
from app.modules.knowledge.vector_width import resize_vector_column

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


async def resize_to_configured_width() -> bool:
    dimensions = get_ai_settings().AI_EMBEDDING_DIMENSIONS
    async with AsyncSessionLocal() as session:
        changed = await resize_vector_column(session, dimensions)
        await session.commit()
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed documents indexed with another embedding model.")
    parser.add_argument(
        "--resize-vector",
        action="store_true",
        help="first resize the pgvector column to AI_EMBEDDING_DIMENSIONS (needed when the model's width changed)",
    )
    args = parser.parse_args()
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    if args.resize_vector:
        changed = asyncio.run(resize_to_configured_width())
        logger.info("vector column %s", "resized" if changed else "already the right width")
    total = asyncio.run(reembed_all())
    logger.info("re-embedding finished: %d documents converted", total)


if __name__ == "__main__":
    main()
