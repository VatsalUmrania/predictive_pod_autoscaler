"""
NEXUS Action Cooldown Store
============================
PostgreSQL-backed action cooldown tracker with in-memory fallback cache.

Design:
    PostgreSQL — primary persistent backend for cooldowns across replicas.
    Memory     — local fallback and fast hydration cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from nexus.db.postgres import PostgresClient, get_database_client

logger = logging.getLogger(__name__)


class CooldownStore:
    """
    PostgreSQL-backed action cooldown tracker with in-memory fallback.

    Args:
        db_path: Deprecated argument kept for backwards compatibility.
        db_client: Optional PostgresClient instance.
        key_prefix: Prefix for all stored keys (default "nexus:cooldown").
    """

    _TABLE = "cooldowns"

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
        key_prefix: str = "nexus:cooldown",
    ) -> None:
        self._db_path = db_path
        self._db_client = db_client
        self._prefix = key_prefix
        # In-memory cache (hydrated from PostgreSQL)
        self._memory: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # Connection
    async def connect(self) -> None:
        """
        Connect to PostgreSQL and hydrate active cooldowns.
        Falls back to purely in-memory if PostgreSQL is unavailable.
        """
        try:
            if self._db_client is None:
                self._db_client = await get_database_client()
            await self._hydrate_memory()
            logger.info("[CooldownStore] PostgreSQL connected and cache hydrated")
        except Exception as exc:
            self._db_client = None
            logger.warning(
                f"[CooldownStore] PostgreSQL unavailable ({exc}) — using in-memory fallback"
            )

    async def _hydrate_memory(self) -> None:
        """Preload unexpired cooldowns into the in-memory cache."""
        if self._db_client is None:
            return
        try:
            async with self._db_client.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT key, expires_at FROM {self._TABLE} WHERE expires_at > $1",
                    time.time(),
                )
            self._memory = {row["key"]: float(row["expires_at"]) for row in rows}
        except Exception as exc:
            logger.warning(f"[CooldownStore] Failed to hydrate cache: {exc}")

    async def close(self) -> None:
        self._db_client = None

    # Key construction
    @staticmethod
    def make_key(runbook_id: str, target: str) -> str:
        """Construct a canonical cooldown key for a runbook + target pair."""
        safe_target = target.replace(" ", "_").replace("/", "::")
        return f"{runbook_id}::{safe_target}"

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    # Core operations
    async def is_in_cooldown(self, key: str) -> bool:
        """Return True if this key is currently in cooldown."""
        full = self._full_key(key)
        now = time.time()
        if self._db_client is not None:
            try:
                async with self._db_client.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT expires_at FROM {self._TABLE} WHERE key = $1",
                        full,
                    )
                if row is None:
                    return False
                expires_at = float(row["expires_at"])
                if expires_at <= now:
                    await self._delete_row(full)
                    return False
                return True
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] PostgreSQL read error: {exc} — using memory"
                )

        # In-memory fallback
        expiry = self._memory.get(full)
        if expiry is None:
            return False
        if now >= expiry:
            self._memory.pop(full, None)
            return False
        return True

    async def set_cooldown(self, key: str, seconds: int) -> None:
        """Mark this key as in-cooldown for the given number of seconds."""
        if seconds <= 0:
            return

        full = self._full_key(key)
        expires_at = time.time() + seconds
        self._memory[full] = expires_at

        if self._db_client is not None:
            try:
                sql = f"""
                INSERT INTO {self._TABLE} (key, expires_at)
                VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET expires_at = EXCLUDED.expires_at
                """
                async with self._db_client.acquire() as conn:
                    await conn.execute(sql, full, float(expires_at))
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] PostgreSQL write error: {exc} — using memory"
                )

    async def clear_cooldown(self, key: str) -> None:
        """Manually clear a cooldown (for testing or admin override)."""
        full = self._full_key(key)
        self._memory.pop(full, None)
        if self._db_client is not None:
            await self._delete_row(full)

    async def _delete_row(self, full_key: str) -> None:
        if self._db_client is None:
            return
        try:
            async with self._db_client.acquire() as conn:
                await conn.execute(
                    f"DELETE FROM {self._TABLE} WHERE key = $1", full_key
                )
        except Exception as exc:
            logger.warning(f"[CooldownStore] PostgreSQL delete error: {exc}")

    async def remaining_seconds(self, key: str) -> float:
        """Return the number of seconds remaining in the cooldown (0 if not in cooldown)."""
        full = self._full_key(key)
        now = time.time()
        if self._db_client is not None:
            try:
                async with self._db_client.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT expires_at FROM {self._TABLE} WHERE key = $1",
                        full,
                    )
                if row is None:
                    return 0.0
                return max(0.0, float(row["expires_at"]) - now)
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] PostgreSQL read error: {exc} — using memory"
                )

        expiry = self._memory.get(full)
        if expiry is None:
            return 0.0
        return max(0.0, expiry - now)

    # Context manager
    async def __aenter__(self) -> CooldownStore:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def __repr__(self) -> str:
        backend = "postgres" if self._db_client is not None else "memory"
        return f"CooldownStore(backend={backend})"
