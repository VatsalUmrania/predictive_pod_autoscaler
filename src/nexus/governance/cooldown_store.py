"""
NEXUS Cooldown Store
====================
Prevents rapid re-execution of the same healing action on the same target.

Backend options:
    SQLite — primary backend. Persists cooldowns to the same ``nexus_audit.db``
             SQLite file as AuditTrail / OutcomeStore, so cooldowns survive
             process restarts (the DB lives on a PersistentVolume in-cluster).
    Memory — fallback when SQLite is unavailable; resets on process restart.

Key format:  nexus:cooldown:{runbook_id}::{target}

Usage:
    store = CooldownStore(db_path="/data/nexus_audit.db")
    await store.connect()

    key = store.make_key("runbook_pod_crashloop_v1", "default/my-pod")

    if await store.is_in_cooldown(key):
        remaining = await store.remaining_seconds(key)
        logger.info(f"Cooldown active: {remaining:.0f}s remaining")
        return

    # ... execute action ...
    await store.set_cooldown(key, seconds=runbook.cooldown_seconds)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

class CooldownStore:
    """
    SQLite-backed action cooldown tracker with in-memory fallback.

    Args:
        db_path: Path to the SQLite database file. Defaults to the same file as
                 the audit trail (``NEXUS_AUDIT_DB_PATH`` env).
        key_prefix: Prefix for all stored keys (default "nexus:cooldown").
    """

    _TABLE = "cooldowns"

    def __init__(
        self,
        db_path: str | None = None,
        key_prefix: str = "nexus:cooldown",
    ) -> None:
        if db_path is None:
            db_path = os.getenv("NEXUS_AUDIT_DB_PATH", "/tmp/nexus_audit.db")
        self._db_path = db_path
        self._prefix = key_prefix
        self._db = None
        # In-memory cache (hydrated from SQLite) — used if SQLite fails at runtime.
        self._memory: dict[str, float] = {}

    # Connection
    async def connect(self) -> None:
        """
        Open the SQLite database and create the cooldowns table.
        Falls back to purely in-memory if SQLite is unavailable.
        """
        import aiosqlite

        try:
            path = Path(self._db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute(
                f"CREATE TABLE IF NOT EXISTS {self._TABLE} ("
                "key TEXT PRIMARY KEY, "
                "expires_at REAL NOT NULL"
                ")"
            )
            await self._db.commit()
            await self._hydrate_memory()
            logger.info(f"[CooldownStore] SQLite connected: {self._db_path}")
        except Exception as exc:
            self._db = None
            logger.warning(
                f"[CooldownStore] SQLite unavailable ({exc}) — using in-memory fallback"
            )

    async def _hydrate_memory(self) -> None:
        """Preload unexpired cooldowns into the in-memory cache."""
        if self._db is None:
            return
        try:
            async with self._db.execute(
                f"SELECT key, expires_at FROM {self._TABLE} WHERE expires_at > ?",
                (time.time(),),
            ) as cur:
                rows = await cur.fetchall()
            self._memory = {row["key"]: row["expires_at"] for row in rows}
        except Exception as exc:
            logger.warning(f"[CooldownStore] Failed to hydrate cache: {exc}")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # Key construction
    @staticmethod
    def make_key(runbook_id: str, target: str) -> str:
        """Construct a canonical cooldown key for a runbook + target pair."""
        # Sanitise target — replace chars that could cause key issues.
        safe_target = target.replace(" ", "_").replace("/", "::")
        return f"{runbook_id}::{safe_target}"

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    # Core operations
    async def is_in_cooldown(self, key: str) -> bool:
        """Return True if this key is currently in cooldown."""
        full = self._full_key(key)
        if self._db is not None:
            try:
                async with self._db.execute(
                    f"SELECT expires_at FROM {self._TABLE} WHERE key = ?",
                    (full,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    return False
                if row["expires_at"] <= time.time():
                    await self._delete_row(full)
                    return False
                return True
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] SQLite read error: {exc} — using memory"
                )

        # In-memory fallback
        expiry = self._memory.get(full)
        if expiry is None:
            return False
        if time.time() >= expiry:
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

        if self._db is not None:
            try:
                await self._db.execute(
                    f"INSERT INTO {self._TABLE} (key, expires_at) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET expires_at = excluded.expires_at",
                    (full, expires_at),
                )
                await self._db.commit()
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] SQLite write error: {exc} — using memory"
                )

    async def clear_cooldown(self, key: str) -> None:
        """Manually clear a cooldown (for testing or admin override)."""
        full = self._full_key(key)
        self._memory.pop(full, None)
        if self._db is not None:
            await self._delete_row(full)

    async def _delete_row(self, full_key: str) -> None:
        try:
            await self._db.execute(
                f"DELETE FROM {self._TABLE} WHERE key = ?", (full_key,)
            )
            await self._db.commit()
        except Exception as exc:
            logger.warning(f"[CooldownStore] SQLite delete error: {exc}")

    async def remaining_seconds(self, key: str) -> float:
        """Return the number of seconds remaining in the cooldown (0 if not in cooldown)."""
        full = self._full_key(key)
        if self._db is not None:
            try:
                async with self._db.execute(
                    f"SELECT expires_at FROM {self._TABLE} WHERE key = ?",
                    (full,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    return 0.0
                return max(0.0, row["expires_at"] - time.time())
            except Exception as exc:
                logger.warning(
                    f"[CooldownStore] SQLite read error: {exc} — using memory"
                )

        expiry = self._memory.get(full)
        if expiry is None:
            return 0.0
        return max(0.0, expiry - time.time())

    # Context manager
    async def __aenter__(self) -> CooldownStore:
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def __repr__(self) -> str:
        backend = "sqlite" if self._db is not None else "memory"
        return f"CooldownStore(backend={backend}, path={self._db_path})"
