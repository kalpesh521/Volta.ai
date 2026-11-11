"""
Retention for chat conversations. Run it at least daily:

    python -m app.agents_platform.maintenance

Deletes conversations idle longer than `AI_CONVERSATION_RETENTION_DAYS`, together with their LangGraph
checkpoints, so the thread-memory tables stay bounded. Checkpoints go first: if the process stops between
the two steps, the conversation row survives and the next run retries, instead of leaving checkpoints that
nothing points to.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents_platform.conversations import ConversationRepository, thread_id_for
from app.ai.checkpointer import CheckpointerProvider
from app.ai.config import get_ai_settings
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging_config import configure_logging

logger = logging.getLogger("volta.agents.maintenance")

_BATCH = 500


async def prune_conversations(
    db: AsyncSession,
    provider: CheckpointerProvider,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete idle conversations and their checkpoints. Returns how many were removed."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    repo = ConversationRepository(db)
    removed = 0
    while True:
        batch = await repo.idle_since(cutoff, limit=_BATCH)
        if not batch:
            return removed
        await provider.delete_threads([thread_id_for(row) for row in batch])
        await repo.delete(batch)
        removed += len(batch)


async def _run() -> int:
    provider = CheckpointerProvider()
    await provider.start()
    try:
        async with AsyncSessionLocal() as session:
            removed = await prune_conversations(
                session, provider, retention_days=get_ai_settings().AI_CONVERSATION_RETENTION_DAYS
            )
            await session.commit()
            return removed
    finally:
        await provider.stop()


def main() -> None:
    configure_logging(settings.LOG_LEVEL, json_logs=settings.LOG_JSON)
    removed = asyncio.run(_run())
    logger.info("conversation maintenance removed=%d", removed)


if __name__ == "__main__":
    main()
