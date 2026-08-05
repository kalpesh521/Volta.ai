"""
Async SQLAlchemy engine + session factory, and the FastAPI dependency
used to inject a DB session into routers/services.
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# asyncpg wants SSL negotiated via connect_args, not a "?ssl=..." query string
# on the URL - keeping it here (driven by one .env flag) means the exact same
# DATABASE_URL format works for both a hosted DB (Neon/Supabase) and a local
# Postgres with no other code changes when you switch between them.
_connect_args = {"ssl": "require"} if settings.DB_SSL_REQUIRED else {}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,  # avoids stale-connection errors after DB restarts/idle timeouts
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency: yields a session, commits on a clean request, and
    rolls back on any exception. Repositories only `flush()` (so callers see
    their own writes mid-request) - this is the single place that actually
    commits the transaction to the database.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
