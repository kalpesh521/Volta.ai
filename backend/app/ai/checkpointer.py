"""
LangGraph checkpointer lifecycle.

Checkpoints hold graph state between steps: a device action waiting for the
user's confirmation lives here. An in-process saver loses every pending
confirmation on restart and is invisible to other replicas, so production uses
Postgres.

    memory    InMemorySaver. Development and tests only.
    postgres  AsyncPostgresSaver on a dedicated schema, with its own connection
              pool so graph traffic cannot starve the OLTP pool.

The provider is started once from the API lifespan and stopped on shutdown.
Create the tables once per deployment, from the migration job:

    python -m app.ai.checkpointer setup
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.engine import make_url

from app.ai.config import AISettings, get_ai_settings
from app.core.config import settings as core_settings

logger = logging.getLogger("volta.ai.checkpointer")

_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_POOL_OPEN_TIMEOUT_SECONDS = 10.0


class CheckpointerError(RuntimeError):
    pass


def libpq_conninfo(database_url: str) -> str:
    """Turn a SQLAlchemy URL (`postgresql+asyncpg://...`) into a libpq URI for psycopg."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise CheckpointerError("The Postgres checkpointer needs a PostgreSQL database URL.")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


class CheckpointerProvider:
    """Owns the saver and, for Postgres, the connection pool behind it."""

    def __init__(self) -> None:
        self._saver: BaseCheckpointSaver | None = None
        self._pool = None
        self._backend = "memory"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def saver(self) -> BaseCheckpointSaver:
        """The active saver. Falls back to an in-process one if `start` was never called (tests)."""
        if self._saver is None:
            self._saver = InMemorySaver()
        return self._saver

    async def start(self, ai: AISettings | None = None, *, auto_setup: bool = False) -> None:
        """Open the configured backend. `auto_setup` creates the tables when missing."""
        if self._saver is not None and self._pool is not None:
            return
        ai = ai or get_ai_settings()
        if ai.AI_CHECKPOINT_BACKEND == "memory":
            self._saver = InMemorySaver()
            self._backend = "memory"
            logger.info("checkpointer backend=memory (state is lost on restart)")
            return
        await self._start_postgres(ai, auto_setup=auto_setup)

    async def _start_postgres(self, ai: AISettings, *, auto_setup: bool) -> None:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        schema = ai.AI_CHECKPOINT_SCHEMA
        if not _SCHEMA_NAME.match(schema):
            raise CheckpointerError(f"AI_CHECKPOINT_SCHEMA {schema!r} is not a valid schema name.")

        conninfo = libpq_conninfo(ai.AI_CHECKPOINT_DATABASE_URL.strip() or core_settings.DATABASE_URL)
        kwargs: dict[str, object] = {
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={schema}",
        }
        if core_settings.DB_SSL_REQUIRED:
            kwargs["sslmode"] = "require"

        if auto_setup:
            await _ensure_schema(conninfo, schema, core_settings.DB_SSL_REQUIRED)

        pool = AsyncConnectionPool(
            conninfo,
            min_size=ai.AI_CHECKPOINT_POOL_MIN,
            max_size=ai.AI_CHECKPOINT_POOL_MAX,
            kwargs=kwargs,
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        try:
            await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
            saver = AsyncPostgresSaver(pool)
            if auto_setup:
                await saver.setup()
        except Exception as exc:
            await pool.close()
            raise CheckpointerError(f"Could not start the Postgres checkpointer: {exc}") from exc

        self._pool, self._saver, self._backend = pool, saver, "postgres"
        logger.info("checkpointer backend=postgres schema=%s", schema)

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
        self._pool, self._saver, self._backend = None, None, "memory"

    async def ping(self) -> bool:
        """True when the backend answers. The in-memory saver is always reachable."""
        if self._pool is None:
            return True
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            logger.exception("checkpointer ping failed")
            return False

    async def delete_threads(self, thread_ids: list[str]) -> None:
        """Drop the checkpoints of finished threads."""
        saver = self.saver
        for thread_id in thread_ids:
            await saver.adelete_thread(thread_id)


async def _ensure_schema(conninfo: str, schema: str, ssl_required: bool) -> None:
    import psycopg
    from psycopg import sql

    extra = {"sslmode": "require"} if ssl_required else {}
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True, **extra) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))


checkpointer_provider = CheckpointerProvider()


async def _setup_cli() -> None:
    ai = get_ai_settings()
    if ai.AI_CHECKPOINT_BACKEND != "postgres":
        print("AI_CHECKPOINT_BACKEND is not 'postgres'; nothing to set up.")
        return
    provider = CheckpointerProvider()
    await provider.start(ai, auto_setup=True)
    await provider.stop()
    print(f"Checkpoint tables are ready in schema {ai.AI_CHECKPOINT_SCHEMA!r}.")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["setup"]:
        print("usage: python -m app.ai.checkpointer setup", file=sys.stderr)
        return 2
    asyncio.run(_setup_cli())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
