"""
Test fixtures.

Uses an in-memory SQLite database (async, via aiosqlite) instead of a real
Postgres instance so the suite is fast and has zero external dependencies.
This works because all models use the portable `GUID` type
(`app/core/types.py`) rather than a Postgres-only column type.

Each test runs inside a SAVEPOINT that's rolled back afterwards, so tests
never leak state into one another (the "transactional rollback per test"
strategy called out in the spec).
"""
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.deps import get_google_oauth_client
from app.core.rate_limit import limiter
from app.models import AuthProvider, PasswordResetToken, RefreshToken, User  # noqa: F401
from main import app

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # keeps the same in-memory DB alive across connections
)
TestSessionLocal = async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _create_schema():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    connection = await test_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)

    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    # Rate-limit state is process-global in slowapi; reset it per test so one
    # test's requests never trip the login/reset rate limit for another test.
    limiter.reset()

    async def _get_test_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_test_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
