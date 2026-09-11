"""
NEXUS SDK App Token Registry
=============================
Maintains authentication tokens for SDK-instrumented applications.

When a developer drops `selfheal.yaml` into their repo, GitAgent calls
`TokenStore.register_app()` to issue a `SELFHEAL_TOKEN`. That token is
embedded into the deployment manifest as an environment variable and
verified by NEXUS on every incoming SDK payload.

Tokens are stored in PostgreSQL.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient, get_database_client

logger = logging.getLogger(__name__)


def _generate_token() -> str:
    """Generate a cryptographically secure SELFHEAL_TOKEN."""
    return "sh_" + secrets.token_urlsafe(32)


class TokenStore:
    """Async PostgreSQL-backed token registry for SDK app authentication."""

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ) -> None:
        self._db_path = db_path
        self._db_client = db_client
        self._cache: dict[str, str] = {}  # token → app_name (in-memory fast-path)
        self._ready = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Initialize connection and preload cache from PostgreSQL."""
        if self._db_client is None:
            self._db_client = await get_database_client()

        try:
            async with self._db_client.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT app_name, token FROM app_tokens WHERE revoked = FALSE"
                )
                for r in rows:
                    self._cache[str(r["token"])] = str(r["app_name"])
            self._ready = True
            logger.info(f"[TokenStore] Initialized — {len(self._cache)} app(s) registered")
        except Exception as exc:
            logger.warning(f"[TokenStore] Cache preload failed: {exc}")

    # ── Registration ──────────────────────────────────────────────────────────

    async def register_app(
        self,
        app_name: str,
        tier: str = "production",
    ) -> str:
        """
        Register a new app and return its SELFHEAL_TOKEN.
        If the app is already registered, returns the existing token.
        """
        existing = await self.get_token(app_name)
        if existing:
            logger.info(
                f"[TokenStore] App '{app_name}' already registered — returning existing token"
            )
            return existing

        token = _generate_token()
        now = datetime.now(timezone.utc)

        if self._db_client is not None:
            sql = """
            INSERT INTO app_tokens (app_name, token, tier, created_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (app_name) DO NOTHING
            """
            async with self._db_client.acquire() as conn:
                await conn.execute(sql, app_name, token, tier, now)

        self._cache[token] = app_name
        logger.info(
            f"[TokenStore] Registered new app '{app_name}' (tier={tier}) "
            f"token={token[:8]}…"
        )
        return token

    # ── Validation ────────────────────────────────────────────────────────────

    async def validate_token(self, token: str) -> str | None:
        """
        Validate an incoming SDK token.
        Returns the app_name if valid, None if not found.
        """
        # Fast path — in-memory cache
        app_name = self._cache.get(token)
        if app_name:
            asyncio.create_task(self._touch(token))
            return app_name

        # Slow path — DB lookup (handles cache miss)
        if self._db_client is not None:
            sql = "SELECT app_name FROM app_tokens WHERE token = $1 AND revoked = FALSE"
            async with self._db_client.acquire() as conn:
                row = await conn.fetchrow(sql, token)
            if row:
                name = str(row["app_name"])
                self._cache[token] = name
                asyncio.create_task(self._touch(token))
                return name

        return None

    async def _touch(self, token: str) -> None:
        """Update last_used and event_count for a token."""
        now = datetime.now(timezone.utc)
        if self._db_client is None:
            return
        try:
            sql = """
            UPDATE app_tokens
            SET last_used = $1, event_count = event_count + 1
            WHERE token = $2
            """
            async with self._db_client.acquire() as conn:
                await conn.execute(sql, now, token)
        except Exception as exc:
            logger.debug(f"[TokenStore] touch failed: {exc}")

    # ── Lookup ────────────────────────────────────────────────────────────────

    async def get_token(self, app_name: str) -> str | None:
        """Return the current token for an app."""
        if self._db_client is None:
            for t, a in self._cache.items():
                if a == app_name:
                    return t
            return None

        sql = "SELECT token FROM app_tokens WHERE app_name = $1 AND revoked = FALSE"
        async with self._db_client.acquire() as conn:
            row = await conn.fetchrow(sql, app_name)
        return str(row["token"]) if row else None

    async def list_apps(self) -> list[dict[str, Any]]:
        """List all registered apps."""
        if self._db_client is None:
            return []

        sql = """
        SELECT app_name, tier, created_at, last_used, event_count, token
        FROM app_tokens
        ORDER BY created_at DESC
        """
        async with self._db_client.acquire() as conn:
            rows = await conn.fetch(sql)

        result = []
        for r in rows:
            ca = r["created_at"].isoformat() if isinstance(r["created_at"], datetime) else str(r["created_at"])
            lu = r["last_used"].isoformat() if isinstance(r["last_used"], datetime) else (str(r["last_used"]) if r["last_used"] else None)
            tok = str(r["token"])
            result.append({
                "app_name": str(r["app_name"]),
                "tier": str(r["tier"]),
                "created_at": ca,
                "last_used": lu,
                "event_count": int(r["event_count"]),
                "token_prefix": tok[:8] + "…",
            })
        return result

    # ── Token rotation ────────────────────────────────────────────────────────

    async def rotate_token(self, app_name: str) -> str:
        """
        Generate a new token for an app and invalidate the old one.
        Returns the new token.
        """
        new_token = _generate_token()
        now = datetime.now(timezone.utc)

        if self._db_client is not None:
            sql = "UPDATE app_tokens SET token = $1, last_used = $2 WHERE app_name = $3"
            async with self._db_client.acquire() as conn:
                await conn.execute(sql, new_token, now, app_name)

        old_entries = [t for t, a in self._cache.items() if a == app_name]
        for t in old_entries:
            del self._cache[t]
        self._cache[new_token] = app_name

        logger.info(f"[TokenStore] Rotated token for '{app_name}'")
        return new_token


# Module-level singleton
_token_store: TokenStore | None = None


def get_token_store() -> TokenStore:
    """Return the global TokenStore singleton (must call await .init() first)."""
    global _token_store
    if _token_store is None:
        _token_store = TokenStore()
    return _token_store
