"""Connection pool management and schema verification for PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PostgresPool:
    """Manages asyncpg connection pool lifecycle and schema loading."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv("NEXUS_POSTGRES_DSN", os.getenv("DATABASE_URL", ""))
        self.min_pool = int(os.getenv("NEXUS_PG_MIN_POOL", "2"))
        self.max_pool = int(os.getenv("NEXUS_PG_MAX_POOL", "10"))
        self._pool: Any = None
        self._lock = asyncio.Lock()

    @property
    def raw_pool(self) -> Any:
        return self._pool

    @asynccontextmanager
    async def acquire(self):
        """Acquire a connection from the pool."""
        if not self._pool:
            raise RuntimeError("PostgresClient connection pool is not initialized.")
        async with self._pool.acquire() as conn:
            yield conn

    async def initialize(
        self, max_retries: int = 6, initial_backoff: float = 1.0
    ) -> None:
        """Create pool with retry backoff and execute schema initialization."""
        import asyncpg

        if not self.dsn:
            raise ValueError(
                "No PostgreSQL DSN configured. Set NEXUS_POSTGRES_DSN or DATABASE_URL."
            )

        last_exc: Exception | None = None
        backoff = initial_backoff
        for attempt in range(1, max_retries + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    self.dsn,
                    min_size=self.min_pool,
                    max_size=self.max_pool,
                    command_timeout=30.0,
                )
                schema_path = Path(__file__).parent / "schema.sql"
                if schema_path.exists():
                    ddl = schema_path.read_text(encoding="utf-8")
                    async with self._pool.acquire() as conn:
                        await conn.execute(ddl)
                logger.info(
                    "[PostgresClient] Initialized connection pool (min=%d, max=%d) and verified schema.",
                    self.min_pool,
                    self.max_pool,
                )
                return
            except Exception as exc:
                last_exc = exc
                if self._pool:
                    try:
                        await self._pool.close()
                    except Exception:
                        pass
                    self._pool = None
                if attempt < max_retries:
                    wait_time = min(backoff, 10.0)
                    logger.warning(
                        "[PostgresClient] Connection attempt %d/%d failed (%s) — retrying in %.1fs...",
                        attempt,
                        max_retries,
                        exc,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    backoff *= 2
                else:
                    logger.error(
                        "[PostgresClient] All %d connection attempts failed: %s",
                        max_retries,
                        exc,
                    )

        raise RuntimeError(
            f"Failed to initialize PostgreSQL pool after {max_retries} attempts: {last_exc}"
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
