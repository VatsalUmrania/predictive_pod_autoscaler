"""
NEXUS Cooldown Store
=====================
Prevents rapid re-execution of the same healing action on the same target.

Backend options:
    Redis  — recommended for production (survives process restarts, shared
             across multiple executor replicas e.g. active-active failover)
    Memory — fallback when Redis is unavailable; resets on process restart

Key format:  nexus:cooldown:{runbook_id}::{target}
TTL format:  set to cooldown_seconds at time of action execution

Usage:
    store = CooldownStore(redis_url="redis://localhost:6379")
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
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CooldownStore:
    """
    Redis-backed action cooldown tracker with in-memory fallback.

    Args:
        redis_url: Redis connection URL (e.g. "redis://localhost:6379").
                   If None or Redis is unavailable, falls back to in-memory.
        key_prefix: Prefix for all Redis keys (default "nexus:cooldown").
    """

    def __init__(
        self,
        redis_url: Optional[str] = None,
        key_prefix: str = "nexus:cooldown",
    ):
        self._redis_url = redis_url
        self._prefix    = key_prefix
        self._redis     = None

        # In-memory fallback: key → expiry monotonic timestamp
        self._memory: Dict[str, float] = {}

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Attempt to connect to Redis.
        Falls back to in-memory silently if Redis is unavailable.
        """
        if not self._redis_url:
            logger.info("[CooldownStore] No Redis URL configured — using in-memory cooldowns")
            return

        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                socket_connect_timeout=3.0,
                socket_timeout=3.0,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info(f"[CooldownStore] Redis connected: {self._redis_url}")
        except Exception as exc:
            logger.warning(
                f"[CooldownStore] Redis unavailable ({exc}) — falling back to in-memory"
            )
            self._redis = None

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()

    # ── Key construction ──────────────────────────────────────────────────────

    @staticmethod
    def make_key(runbook_id: str, target: str) -> str:
        """Construct a canonical cooldown key for a runbook + target pair."""
        # Sanitise target — replace chars that could cause Redis key issues
        safe_target = target.replace(" ", "_").replace("/", "::")
        return f"{runbook_id}::{safe_target}"

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    # ── Core operations ───────────────────────────────────────────────────────

    async def is_in_cooldown(self, key: str) -> bool:
        """Return True if this key is currently in cooldown."""
        if self._redis:
            try:
                return await self._redis.exists(self._full_key(key)) > 0
            except Exception as exc:
                logger.warning(f"[CooldownStore] Redis read error: {exc} — using memory")

        # In-memory fallback
        expiry = self._memory.get(key)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            self._memory.pop(key, None)
            return False
        return True

    async def set_cooldown(self, key: str, seconds: int) -> None:
        """Mark this key as in-cooldown for the given number of seconds."""
        if seconds <= 0:
            return

        if self._redis:
            try:
                await self._redis.setex(self._full_key(key), seconds, "1")
                return
            except Exception as exc:
                logger.warning(f"[CooldownStore] Redis write error: {exc} — using memory")

        # In-memory fallback
        self._memory[key] = time.monotonic() + seconds

    async def clear_cooldown(self, key: str) -> None:
        """Manually clear a cooldown (for testing or admin override)."""
        if self._redis:
            try:
                await self._redis.delete(self._full_key(key))
            except Exception:
                pass
        self._memory.pop(key, None)

    async def remaining_seconds(self, key: str) -> float:
        """Return the number of seconds remaining in the cooldown (0 if not in cooldown)."""
        if self._redis:
            try:
                ttl = await self._redis.ttl(self._full_key(key))
                return max(0.0, float(ttl))
            except Exception:
                pass

        expiry = self._memory.get(key)
        if expiry is None:
            return 0.0
        return max(0.0, expiry - time.monotonic())

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "CooldownStore":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    def __repr__(self) -> str:
        backend = "redis" if self._redis else "memory"
        return f"CooldownStore(backend={backend}, entries={len(self._memory)})"
