"""Unit tests for nexus.governance.cooldown_store — SQLite backend.

Tests that cooldowns persist to the same ``nexus_audit.db`` SQLite file as
AuditTrail / OutcomeStore, so they survive a NEXUS restart even without Redis:

  - connect() creates the ``cooldowns`` table and hydrates the in-memory cache
  - set_cooldown / is_in_cooldown / remaining_seconds / clear_cooldown round-trip
  - persisted cooldowns survive a reconnect (new store instance, same db_path)
  - expired entries are dropped on read
  - when SQLite is unavailable, the store degrades to purely in-memory

The ``aiosqlite`` connector is patched to force the fallback path where noted.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.governance.cooldown_store import CooldownStore


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "nexus_audit.db")


@pytest.mark.asyncio
async def test_connect_creates_table(db_path: str):
    """connect() opens the DB and creates the cooldowns table."""
    store = CooldownStore(db_path=db_path)
    await store.connect()
    assert store._db is not None
    await store.close()

    # Table persisted on disk.
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cooldowns'"
    )
    assert await cur.fetchone() is not None
    await conn.close()


@pytest.mark.asyncio
async def test_set_is_in_cooldown_round_trip(db_path: str):
    """set_cooldown → is_in_cooldown True, then false after expiry window cleared."""
    store = CooldownStore(db_path=db_path)
    await store.connect()

    key = store.make_key("rb", "target")
    assert await store.is_in_cooldown(key) is False

    await store.set_cooldown(key, seconds=30)
    assert await store.is_in_cooldown(key) is True
    assert await store.remaining_seconds(key) > 0

    await store.clear_cooldown(key)
    assert await store.is_in_cooldown(key) is False
    await store.close()


@pytest.mark.asyncio
async def test_cooldown_survives_reconnect(db_path: str):
    """A cooldown set in one process survives a brand-new store/connection."""
    store = CooldownStore(db_path=db_path)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=120)
    await store.close()

    # New store instance against the same DB — cooldown must still hold.
    store2 = CooldownStore(db_path=db_path)
    await store2.connect()
    assert await store2.is_in_cooldown(key) is True
    assert await store2.remaining_seconds(key) > 0
    await store2.close()


@pytest.mark.asyncio
async def test_expired_entry_pruned_on_read(db_path: str):
    """A cooldown whose expiry has passed is treated as not-in-cooldown and removed."""
    store = CooldownStore(db_path=db_path)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=1)
    assert await store.is_in_cooldown(key) is True

    await asyncio.sleep(1.2)
    assert await store.is_in_cooldown(key) is False
    assert await store.remaining_seconds(key) == 0.0
    await store.close()


@pytest.mark.asyncio
async def test_zero_seconds_does_not_set(db_path: str):
    """seconds <= 0 is a no-op — nothing is persisted."""
    store = CooldownStore(db_path=db_path)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=0)
    assert await store.is_in_cooldown(key) is False
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_unavailable_degrades_to_memory(db_path: str):
    """If SQLite init fails, the store works purely in-memory."""
    with patch("aiosqlite.connect", side_effect=RuntimeError("disk full")):
        store = CooldownStore(db_path=db_path)
        await store.connect()

    assert store._db is None
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=30)
    assert await store.is_in_cooldown(key) is True
    assert await store.remaining_seconds(key) > 0
    await store.close()


@pytest.mark.asyncio
async def test_db_path_from_env(monkeypatch, tmp_path: Path):
    """With no db_path arg, the store uses NEXUS_AUDIT_DB_PATH."""
    p = str(tmp_path / "env.db")
    monkeypatch.setenv("NEXUS_AUDIT_DB_PATH", p)
    store = CooldownStore()
    await store.connect()
    assert store._db_path == p
    assert store._db is not None
    await store.close()