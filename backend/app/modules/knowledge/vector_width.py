"""
The width of the pgvector column (tracker 3.9).

`knowledge_chunks.embedding_vec` was created as `vector(256)`, the size of the built-in hashing embedder. A real
embedding model has its own size (384, 768, 1536, 3072), and Postgres will not store a vector of another width. This
resizes the column to match `AI_EMBEDDING_DIMENSIONS`, as an operations step rather than a migration, because the right
number depends on a model choice that is made per deployment:

    python -m app.modules.knowledge.reembed --resize-vector

Resizing empties the column (a vector of one width cannot be cast to another) and rebuilds the HNSW index; the
`reembed` run that follows refills it. Until then search still works: the JSON embeddings are the source of truth and
the full-text search is unaffected, so a half-finished resize degrades retrieval quality, never availability.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("volta.knowledge.vector")

INDEX = "ix_knowledge_chunks_embedding_vec"
MAX_HNSW_DIMENSIONS = 2000  # pgvector's limit for an HNSW index on the `vector` type


class VectorWidthError(ValueError):
    pass


async def current_width(db: AsyncSession) -> int | None:
    """Width of the column, or None when it does not exist (pgvector was not available at migration time)."""
    result = await db.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding_vec' AND NOT attisdropped"
        )
    )
    width = result.scalar_one_or_none()
    return None if width is None or width < 0 else int(width)


async def resize_vector_column(db: AsyncSession, dimensions: int) -> bool:
    """Make the column `vector(dimensions)`. Returns True when it changed, False when it was already that width."""
    if dimensions < 1:
        raise VectorWidthError("the embedding width must be positive")
    if dimensions > MAX_HNSW_DIMENSIONS:
        raise VectorWidthError(
            f"{dimensions} dimensions is more than an HNSW index can hold ({MAX_HNSW_DIMENSIONS}); "
            "choose an embedding model with a smaller output, or one that supports truncation"
        )
    width = await current_width(db)
    if width is None:
        raise VectorWidthError("knowledge_chunks.embedding_vec does not exist: is the pgvector extension installed?")
    if width == dimensions:
        return False
    await db.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
    await db.execute(text(f"ALTER TABLE knowledge_chunks ALTER COLUMN embedding_vec TYPE vector({int(dimensions)}) USING NULL"))
    await db.execute(text(f"CREATE INDEX {INDEX} ON knowledge_chunks USING hnsw (embedding_vec vector_cosine_ops)"))
    logger.warning("knowledge vector column resized from %s to %d dimensions; run reembed to refill it", width, dimensions)
    return True
