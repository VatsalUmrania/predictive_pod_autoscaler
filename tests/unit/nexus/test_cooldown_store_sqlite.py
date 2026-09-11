"""Unit tests for nexus.governance.cooldown_store — PostgreSQL backend & memory fallback.

Tests that cooldowns persist to the PostgreSQL ``cooldowns`` table:
  - connect() hydrates the in-memory cache from PostgreSQL
  - set_cooldown / is_in_cooldown / remaining_seconds / clear_cooldown round-trip
  - persisted cooldowns survive a reconnect (new store instance, same DB client)
  - expired entries are dropped on read
  - when PostgreSQL is unavailable, the store degrades to purely in-memory
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from nexus.governance.cooldown_store import CooldownStore
from tests.unit.nexus.test_db_helpers import MockPostgresClient


@pytest.fixture
def mock_client() -> MockPostgresClient:
    return MockPostgresClient()


@pytest.mark.asyncio
async def test_connect_and_hydrate(mock_client: MockPostgresClient):
    """connect() connects to PostgreSQL and hydrates unexpired cooldowns."""
    full_key = "nexus:cooldown:rb::target"
    mock_client._cooldowns[full_key] = time.time() + 60.0

    store = CooldownStore(db_client=mock_client)
    await store.connect()
    assert store._db_client is not None
    assert full_key in store._memory
    assert await store.is_in_cooldown("rb::target") is True
    await store.close()


@pytest.mark.asyncio
async def test_set_is_in_cooldown_round_trip(mock_client: MockPostgresClient):
    """set_cooldown → is_in_cooldown True, then False after expiry window cleared."""
    store = CooldownStore(db_client=mock_client)
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
async def test_cooldown_survives_reconnect(mock_client: MockPostgresClient):
    """A cooldown set in one process survives a brand-new store/connection against the same DB."""
    store = CooldownStore(db_client=mock_client)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=120)
    await store.close()

    # New store instance against the same DB — cooldown must still hold.
    store2 = CooldownStore(db_client=mock_client)
    await store2.connect()
    assert await store2.is_in_cooldown(key) is True
    assert await store2.remaining_seconds(key) > 0
    await store2.close()


@pytest.mark.asyncio
async def test_expired_entry_pruned_on_read(mock_client: MockPostgresClient):
    """A cooldown whose expiry has passed is treated as not-in-cooldown and removed."""
    store = CooldownStore(db_client=mock_client)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=1)
    assert await store.is_in_cooldown(key) is True

    await asyncio.sleep(1.2)
    assert await store.is_in_cooldown(key) is False
    assert await store.remaining_seconds(key) == 0.0
    await store.close()


@pytest.mark.asyncio
async def test_zero_seconds_does_not_set(mock_client: MockPostgresClient):
    """seconds <= 0 is a no-op — nothing is persisted."""
    store = CooldownStore(db_client=mock_client)
    await store.connect()
    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=0)
    assert await store.is_in_cooldown(key) is False
    await store.close()


@pytest.mark.asyncio
async def test_postgres_unavailable_degrades_to_memory():
    """If PostgreSQL init fails, the store works purely in-memory."""
    with patch(
        "nexus.governance.cooldown_store.get_database_client",
        side_effect=RuntimeError("connection refused"),
    ):
        store = CooldownStore()
        await store.connect()

    assert store._db_client is None
    key = store.make_key("rb", "target")
    assert await store.is_in_cooldown(key) is False

    await store.set_cooldown(key, seconds=30)
    assert await store.is_in_cooldown(key) is True
    assert await store.remaining_seconds(key) > 0

    await store.clear_cooldown(key)
    assert await store.is_in_cooldown(key) is False
    await store.close()

