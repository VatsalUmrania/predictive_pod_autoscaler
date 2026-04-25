"""
NEXUS Token Store
==================
Maps SELFHEAL_TOKEN → app_name for SDK authentication.

Tokens are auto-generated (UUID4) when a new app's selfheal.yaml is first
detected by GitAgent. They're stored in SQLite alongside the AuditTrail.

Schema:
    app_tokens (
        app_name    TEXT PRIMARY KEY,
        token       TEXT UNIQUE NOT NULL,
        tier        TEXT DEFAULT 'production',
        created_at  TEXT NOT NULL,
        last_used   TEXT,
        event_count INTEGER DEFAULT 0
    )

Usage:
    store = TokenStore(db_path="data/nexus_knowledge.db")
    await store.init()

    # Register a new app (returns existing token if already registered)
    token = await store.register_app("checkout-service", tier="production")

    # Validate an incoming SDK request
    app_name = await store.validate_token(token)   # → "checkout-service" or None

    # Rotate if compromised
    new_token = await store.rotate_token("checkout-service")
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.getenv("NEXUS_KNOWLEDGE_DB_PATH", "data/nexus_knowledge.db")


class TokenStore:
    """Async SQLite-backed token registry for SDK app authentication."""

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self._db_path   = db_path
        self._cache:    Dict[str, str] = {}   # token → app_name (in-memory fast-path)
        self._ready     = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Create the app_tokens table if it doesn't exist."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS app_tokens (
                    app_name    TEXT PRIMARY KEY,
                    token       TEXT UNIQUE NOT NULL,
                    tier        TEXT DEFAULT 'production',
                    created_at  TEXT NOT NULL,
                    last_used   TEXT,
                    event_count INTEGER DEFAULT 0
                )
            """)
            await db.commit()

            # Pre-load cache
            async with db.execute("SELECT app_name, token FROM app_tokens") as cur:
                async for row in cur:
                    self._cache[row[1]] = row[0]

        self._ready = True
        logger.info(
            f"[TokenStore] Initialized — {len(self._cache)} app(s) registered"
        )

    # ── Registration ──────────────────────────────────────────────────────────

    async def register_app(
        self,
        app_name: str,
        tier: str = "production",
    ) -> str:
        """
        Register a new app and return its SELFHEAL_TOKEN.
        If the app is already registered, returns the existing token.

        Called by GitAgent when selfheal.yaml is first detected.
        """
        # Check if already registered
        existing = await self.get_token(app_name)
        if existing:
            logger.info(f"[TokenStore] App '{app_name}' already registered — returning existing token")
            return existing

        token = _generate_token()
        now   = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO app_tokens (app_name, token, tier, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (app_name, token, tier, now),
            )
            await db.commit()

        self._cache[token] = app_name
        logger.info(
            f"[TokenStore] Registered new app '{app_name}' (tier={tier}) "
            f"token={token[:8]}…"
        )
        return token

    # ── Validation ────────────────────────────────────────────────────────────

    async def validate_token(self, token: str) -> Optional[str]:
        """
        Validate an incoming SDK token.

        Returns the app_name if valid, None if not found.
        Updates last_used + event_count on success (non-blocking via background).
        """
        # Fast path — in-memory cache
        app_name = self._cache.get(token)
        if app_name:
            # Fire-and-forget update (don't block the request)
            import asyncio
            asyncio.create_task(self._touch(token))
            return app_name

        # Slow path — DB lookup (handles cache miss after restart)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT app_name FROM app_tokens WHERE token = ?", (token,)
            ) as cur:
                row = await cur.fetchone()

        if row:
            self._cache[token] = row[0]
            import asyncio
            asyncio.create_task(self._touch(token))
            return row[0]

        return None

    async def _touch(self, token: str) -> None:
        """Update last_used and event_count for a token."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE app_tokens
                    SET last_used = ?, event_count = event_count + 1
                    WHERE token = ?
                    """,
                    (now, token),
                )
                await db.commit()
        except Exception as exc:
            logger.debug(f"[TokenStore] touch failed: {exc}")

    # ── Lookup ────────────────────────────────────────────────────────────────

    async def get_token(self, app_name: str) -> Optional[str]:
        """Return the current token for an app (for display / ops use)."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT token FROM app_tokens WHERE app_name = ?", (app_name,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def list_apps(self) -> List[Dict]:
        """List all registered apps (for /apps endpoint)."""
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """
                SELECT app_name, tier, created_at, last_used, event_count
                FROM app_tokens
                ORDER BY created_at DESC
                """
            ) as cur:
                rows = await cur.fetchall()
        return [
            {
                "app_name":    r[0],
                "tier":        r[1],
                "created_at":  r[2],
                "last_used":   r[3],
                "event_count": r[4],
                "token_prefix": (await self.get_token(r[0]) or "")[:8] + "…",
            }
            for r in rows
        ]

    # ── Token rotation ────────────────────────────────────────────────────────

    async def rotate_token(self, app_name: str) -> str:
        """
        Generate a new token for an app and invalidate the old one.
        Returns the new token.
        """
        new_token = _generate_token()
        now       = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE app_tokens SET token = ?, last_used = ? WHERE app_name = ?",
                (new_token, now, app_name),
            )
            await db.commit()

        # Invalidate cache entries for this app
        old_entries = [t for t, a in self._cache.items() if a == app_name]
        for t in old_entries:
            del self._cache[t]
        self._cache[new_token] = app_name

        logger.info(f"[TokenStore] Rotated token for '{app_name}'")
        return new_token


# ──────────────────────────────────────────────────────────────────────────────
# Token generation
# ──────────────────────────────────────────────────────────────────────────────

def _generate_token() -> str:
    """Generate a cryptographically secure SELFHEAL_TOKEN."""
    return "sh_" + secrets.token_urlsafe(32)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────

_token_store: Optional[TokenStore] = None


def get_token_store() -> TokenStore:
    """Return the global TokenStore singleton (must call await .init() first)."""
    global _token_store
    if _token_store is None:
        _token_store = TokenStore()
    return _token_store
