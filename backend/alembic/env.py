"""
Alembic environment configuration.

Pulls the DB URL from our app's `Settings` (i.e. `.env`) instead of
`alembic.ini`, so there's a single source of truth for the connection
string. Runs migrations with an async engine since the app uses async
SQLAlchemy end-to-end.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings
from app.core.database import Base
from app.models import AuthProvider, PasswordResetToken, RefreshToken, User  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares against this metadata to detect model changes.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # Same SSL toggle as app/core/database.py - keeps Neon/Supabase and local
    # Postgres working against the identical DATABASE_URL format.
    connect_args = {"ssl": "require"} if settings.DB_SSL_REQUIRED else {}

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
