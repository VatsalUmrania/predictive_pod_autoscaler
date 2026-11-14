"""
NEXUS SDK App Token Registry
=============================
Maintains authentication tokens for SDK-instrumented applications.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from nexus.db.postgres import PostgresClient, get_database_client
from nexus.integration.token_utils import format_app_row, generate_token

logger = logging.getLogger(__name__)

_generate_token = generate_token


class TokenStore:
    """Async PostgreSQL-backed token registry for SDK app authentication."""

    def __init__(
        self,
        db_path: str | None = None,
        db_client: PostgresClient | None = None,
    ) -> None:
        self._db_path = db_path
        self._db_client = db_client
        self._cache: dict[str, str] = {}
        self._ready = False

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
            logger.info(
                f"[TokenStore] Initialized — {len(self._cache)} app(s) registered"
            )
        except Exception as exc:
            logger.warning(f"[TokenStore] Cache preload failed: {exc}")

    async def register_app(
        self,
        app_name: str,
        tier: str = "production",
    ) -> str:
        """Register a new app and return its SELFHEAL_TOKEN."""
        existing = await self.get_token(app_name)
        if existing:
            logger.info(
                f"[TokenStore] App '{app_name}' already registered — returning existing token"
            )
            return existing

        token = generate_token()
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

    async def validate_token(self, token: str) -> str | None:
        """Validate an incoming SDK token."""
        app_name = self._cache.get(token)
        if app_name:
            asyncio.create_task(self._touch(token))
            return app_name

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

        return [format_app_row(r) for r in rows]

    async def rotate_token(self, app_name: str) -> str:
        """Generate a new token for an app and invalidate the old one."""
        new_token = generate_token()
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


_token_store: TokenStore | None = None


def get_token_store() -> TokenStore:
    """Return the global TokenStore singleton (must call await .init() first)."""
    global _token_store
    if _token_store is None:
        _token_store = TokenStore()
    return _token_store
