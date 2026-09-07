"""
NEXUS LangGraph Checkpointer
============================
PostgreSQL-backed LangGraph checkpoint store (AsyncPostgresSaver) with
graceful in-memory fallback (MemorySaver) for tests and local development.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


def resolve_dsn() -> str | None:
    """Resolve PostgreSQL connection string from standard NEXUS env vars."""
    dsn = os.getenv("NEXUS_DB_DSN") or os.getenv("DATABASE_URL")
    if dsn:
        return dsn

    host = os.getenv("NEXUS_PG_HOST") or os.getenv("POSTGRES_HOST")
    if host:
        port = os.getenv("NEXUS_PG_PORT", "5432")
        user = os.getenv("NEXUS_PG_USER", "postgres")
        password = os.getenv("NEXUS_PG_PASSWORD", "postgres")
        dbname = os.getenv("NEXUS_PG_DATABASE", "nexus")
        return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    return None


class GraphCheckpointStore:
    """Manages the lifecycle of the LangGraph state checkpointer."""

    def __init__(self, saver: Any = None) -> None:
        self.saver = saver or MemorySaver()
        self._is_postgres = False

    @classmethod
    def from_env(cls) -> GraphCheckpointStore:
        """Instantiate checkpointer from environment settings."""
        backend = os.getenv("NEXUS_GRAPH_CHECKPOINTER", "auto").lower()

        if backend == "memory":
            logger.info("[GraphCheckpointStore] Using MemorySaver (explicitly requested)")
            return cls(MemorySaver())

        dsn = resolve_dsn()
        if dsn and backend in ("auto", "postgres"):
            try:
                # AsyncPostgresSaver will be initialized during start()
                store = cls(MemorySaver())
                store._dsn = dsn
                store._is_postgres = True
                return store
            except Exception as exc:
                logger.warning(
                    "[GraphCheckpointStore] Could not prepare Postgres checkpointer: %s. Falling back to MemorySaver",
                    exc,
                )

        logger.info("[GraphCheckpointStore] Using MemorySaver default")
        return cls(MemorySaver())

    async def start(self) -> None:
        """Initialize connection pool and tables if using PostgreSQL."""
        if getattr(self, "_is_postgres", False):
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                # In production, AsyncPostgresSaver.from_conn_string creates the pool
                # For safety, check if from_conn_string or constructor is supported
                if hasattr(AsyncPostgresSaver, "from_conn_string"):
                    # Use connection string
                    self.saver = AsyncPostgresSaver.from_conn_string(self._dsn)
                    if hasattr(self.saver, "setup"):
                        await self.saver.setup()
                    logger.info("[GraphCheckpointStore] Initialized AsyncPostgresSaver on %s", self._dsn)
            except Exception as exc:
                logger.warning(
                    "[GraphCheckpointStore] Failed to connect AsyncPostgresSaver (%s). Using MemorySaver fallback.",
                    exc,
                )
                self.saver = MemorySaver()
                self._is_postgres = False

    async def stop(self) -> None:
        """Close connections gracefully."""
        pass
