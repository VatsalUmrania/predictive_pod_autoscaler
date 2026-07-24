"""Unit tests for nexus.governance.cooldown_store — K8s ConfigMap fallback.

Tests the change that replaced the ephemeral in-memory-only fallback with a
Kubernetes ConfigMap backend, so cooldowns survive a NEXUS restart even when
Redis is unavailable:

  - connect() with no Redis URL tries the ConfigMap fallback
  - if K8s is unavailable, it degrades to purely in-memory (the old behavior)
  - _load_from_k8s reads/parses existing state and prunes expired entries
  - _load_from_k8s creates the ConfigMap on 404
  - set_cooldown/clear_cooldown schedule a sync to the ConfigMap
  - _sync_to_k8s serializes unexpired entries (mono→unix) and patches the CM

The ``kubernetes`` SDK is mocked wholesale via sys.modules so the lazy imports
inside the store see a controlled client.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from nexus.governance.cooldown_store import CooldownStore

# ── K8s SDK mock harness ───────────────────────────────────────────────────────


class _V1ObjectMeta:
    """Lightweight stand-in for kubernetes.client.V1ObjectMeta (name-only)."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name


class _V1ConfigMap:
    """Lightweight stand-in for kubernetes.client.V1ConfigMap (metadata+data)."""

    def __init__(
        self, metadata: _V1ObjectMeta | None = None, data: dict | None = None
    ) -> None:
        self.metadata = metadata
        self.data = data


def _install_fake_k8s(monkeypatch):
    """Inject a fake ``kubernetes`` SDK into sys.modules.

    Returns a table of handles: ``v1`` (CoreV1Api instance), ``k8s`` (module),
    ``api_exc_cls`` (ApiException class used by the 404 branch).
    """
    v1 = MagicMock()
    v1.read_namespaced_config_map = MagicMock()
    v1.create_namespaced_config_map = MagicMock()
    v1.patch_namespaced_config_map = MagicMock()

    api_exc_cls = type("ApiException", (Exception,), {})
    rest_mod = MagicMock()
    rest_mod.ApiException = api_exc_cls

    client_mod = MagicMock()
    client_mod.CoreV1Api = MagicMock(return_value=v1)
    client_mod.V1ConfigMap = _V1ConfigMap
    client_mod.V1ObjectMeta = _V1ObjectMeta
    client_mod.rest = rest_mod

    config_mod = MagicMock()

    # The source reads ``config.ConfigException`` by attribute in its inner
    # except, so the mock attribute must be named ``ConfigException``. The class
    # itself needs an ``Error`` suffix for pep8-naming (N818).
    class ConfigExceptionError(Exception):
        pass

    config_mod.ConfigException = ConfigExceptionError
    config_mod.load_incluster_config = MagicMock()
    config_mod.load_kube_config = MagicMock()

    k8s_mod = MagicMock()
    k8s_mod.config = config_mod
    k8s_mod.client = client_mod
    k8s_mod.client.rest = rest_mod

    monkeypatch.setitem(sys.modules, "kubernetes", k8s_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.config", config_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_mod)
    return {"v1": v1, "k8s": k8s_mod, "config": config_mod, "api_exc_cls": api_exc_cls}


# ── connect() fallback selection ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_from_k8s_non_404_api_exception_degrades(monkeypatch):
    """A non-404 ApiException on ConfigMap read degrades to in-memory
    (the outer try/except in _init_k8s_fallback swallows it)."""
    h = _install_fake_k8s(monkeypatch)
    api_exc = h["api_exc_cls"]()  # ApiException with a server-error status
    api_exc.status = 500  # not 404 → _load_from_k8s re-raises (else branch)
    h["v1"].read_namespaced_config_map.side_effect = api_exc

    store = CooldownStore(redis_url=None)
    with patch("nexus.governance.cooldown_store.CooldownStore._sync_to_k8s") as sync:
        await store.connect()  # must NOT raise

    # to_thread re-raises into the outer except Exception → fallback disabled.
    assert store._k8s_available is False
    sync.assert_not_called()


@pytest.mark.asyncio
async def test_connect_no_redis_happy_path_makes_k8s_available(monkeypatch):
    """No Redis URL + K8s reachable → ConfigMap fallback is initialized."""
    _install_fake_k8s(monkeypatch)

    store = CooldownStore(redis_url=None)
    with patch("nexus.governance.cooldown_store.CooldownStore._sync_to_k8s") as sync:
        await store.connect()

    assert store._k8s_available is True
    assert store._redis is None
    sync.assert_not_called()  # connect() itself never schedules a sync


@pytest.mark.asyncio
async def test_connect_no_redis_no_k8s_degrades_to_in_memory(monkeypatch):
    """K8s reachable but load errors → connect falls back purely in-memory."""
    h = _install_fake_k8s(monkeypatch)
    # load_incluster_config raises a generic error (not ConfigException) → the
    # inner except won't catch → propagates to _init_k8s_fallback's outer except.
    h["config"].load_incluster_config.side_effect = RuntimeError(
        "no incluster kubeconfig"
    )

    store = CooldownStore(redis_url=None)
    await store.connect()

    assert store._k8s_available is False
    assert store._redis is None


@pytest.mark.asyncio
async def test_connect_uses_incluster_then_kube_config(monkeypatch):
    """load_incluster_config raising ConfigException falls back to load_kube_config."""
    h = _install_fake_k8s(monkeypatch)
    h["config"].load_incluster_config.side_effect = h["config"].ConfigException()
    # load_kube_config succeeds (default MagicMock) → _k8s_available True.
    store = CooldownStore(redis_url=None)
    await store.connect()
    h["config"].load_kube_config.assert_called_once()
    assert store._k8s_available is True


# ── _load_from_k8s ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_from_k8s_hydrates_unexpired_cooldowns(monkeypatch):
    """Existing unexpired entries in the CM are loaded into memory."""
    future = time.time() + 100  # 100s in the future
    h = _install_fake_k8s(monkeypatch)
    h["v1"].read_namespaced_config_map.return_value = MagicMock(
        data={"state.json": json.dumps({"rb::target": future})}
    )

    store = CooldownStore(redis_url=None)
    await store.connect()

    # The CM key is stored verbatim ("rb::target"); assert it hydrated and repeats.
    assert "rb::target" in store._memory
    assert await store.is_in_cooldown("rb::target") is True


@pytest.mark.asyncio
async def test_load_from_k8s_drops_expired_cooldowns(monkeypatch):
    """Past-expiry entries in the CM are not hydrated into memory."""
    past = time.time() - 50  # expired
    h = _install_fake_k8s(monkeypatch)
    h["v1"].read_namespaced_config_map.return_value = MagicMock(
        data={"state.json": json.dumps({"rb::target": past})}
    )

    store = CooldownStore(redis_url=None)
    await store.connect()

    assert "rb::target" not in store._memory
    assert await store.is_in_cooldown("rb::target") is False


@pytest.mark.asyncio
async def test_load_from_k8s_creates_cm_on_404(monkeypatch):
    """A missing ConfigMap is created (empty state), not treated as an error."""
    h = _install_fake_k8s(monkeypatch)
    api_exc = h["api_exc_cls"]()
    api_exc.status = 404
    h["v1"].read_namespaced_config_map.side_effect = api_exc

    store = CooldownStore(redis_url=None)
    await store.connect()

    h["v1"].read_namespaced_config_map.assert_called_once()
    assert h["v1"].create_namespaced_config_map.call_count == 1
    # create(ns, body)
    args, _ = h["v1"].create_namespaced_config_map.call_args
    assert args[0] == "default"
    assert args[1].data == {"state.json": "{}"}
    assert args[1].metadata.name == "nexus-cooldown-state"
    assert store._k8s_available is True


@pytest.mark.asyncio
async def test_load_from_k8s_no_state_json_is_safe(monkeypatch):
    """A CM with no data or no state.json key loads nothing (no crash)."""
    h = _install_fake_k8s(monkeypatch)
    h["v1"].read_namespaced_config_map.return_value = MagicMock(data=None)

    store = CooldownStore(redis_url=None)
    await store.connect()
    assert store._memory == {}


# ── set_cooldown / clear_cooldown sync triggers ───────────────────────────────


@pytest.mark.asyncio
async def test_set_cooldown_schedules_configmap_sync(monkeypatch):
    """set_cooldown schedules a background _sync_to_k8s when K8s is available."""
    _install_fake_k8s(monkeypatch)
    store = CooldownStore(redis_url=None)
    await store.connect()

    fired = asyncio.Event()

    def _sentinel_sync():
        fired.set()

    store._sync_to_k8s = _sentinel_sync  # type: ignore[method-assign]

    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=30)
    # Fire-and-forget sync task runs within a tick.
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert await store.is_in_cooldown(key) is True


@pytest.mark.asyncio
async def test_set_cooldown_no_sync_when_k8s_unavailable(monkeypatch):
    """Without K8s, set_cooldown skips scheduling any _sync_to_k8s."""
    h = _install_fake_k8s(monkeypatch)
    h["config"].load_incluster_config.side_effect = RuntimeError("no kubeconfig")
    store = CooldownStore(redis_url=None)
    await store.connect()
    assert store._k8s_available is False

    sentinel = MagicMock()
    store._sync_to_k8s = sentinel  # type: ignore[method-assign]

    key = store.make_key("rb", "target")
    await store.set_cooldown(key, seconds=30)
    await asyncio.sleep(0.05)  # let any stray task run

    sentinel.assert_not_called()
    assert await store.is_in_cooldown(key) is True


@pytest.mark.asyncio
async def test_clear_cooldown_schedules_configmap_sync(monkeypatch):
    """clear_cooldown also schedules a sync so the CM reflects the removal."""
    _install_fake_k8s(monkeypatch)
    store = CooldownStore(redis_url=None)
    await store.connect()

    key = store.make_key("rb", "target")
    # Set a cooldown directly (drain its sync first) then clear with a sentinel.
    store._memory[key] = time.monotonic() + 60
    await asyncio.sleep(0.02)

    fired = asyncio.Event()

    def _sentinel_sync():
        fired.set()

    store._sync_to_k8s = _sentinel_sync  # type: ignore[method-assign]
    await store.clear_cooldown(key)

    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert await store.is_in_cooldown(key) is False


# ── _sync_to_k8s serialization ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_to_k8s_serializes_unexpired_entries(monkeypatch):
    """_sync_to_k8s converts monotonic expiries → unix and patches the CM."""
    h = _install_fake_k8s(monkeypatch)
    store = CooldownStore(redis_url=None)
    await store.connect()
    h["v1"].patch_namespaced_config_map.reset_mock()

    key = "rb::target"
    expired_key = "rb::gone"
    store._memory[key] = time.monotonic() + 60
    store._memory[expired_key] = time.monotonic() - 1  # already expired

    store._sync_to_k8s()

    h["v1"].patch_namespaced_config_map.assert_called_once()
    args, _ = h["v1"].patch_namespaced_config_map.call_args
    # patch(name, namespace, body)
    assert args[0] == "nexus-cooldown-state"
    assert args[1] == "default"
    state = json.loads(args[2].data["state.json"])
    assert key in state
    assert expired_key not in state  # expired entry pruned
    assert state[key] > time.time()  # unix expiry still in the future
    assert expired_key not in store._memory  # evicted from in-memory dict too


@pytest.mark.asyncio
async def test_sync_to_k8s_logs_on_patch_failure(monkeypatch):
    """A patch failure is logged (not raised) — _sync_to_k8s must not throw."""
    h = _install_fake_k8s(monkeypatch)
    store = CooldownStore(redis_url=None)
    await store.connect()
    store._memory["k"] = time.monotonic() + 5
    h["v1"].patch_namespaced_config_map.side_effect = RuntimeError("api server down")

    store._sync_to_k8s()  # must not raise
    h["v1"].patch_namespaced_config_map.assert_called_once()


@pytest.mark.asyncio
async def test_sync_to_k8s_no_op_when_k8s_unavailable(monkeypatch):
    """No K8s → _sync_to_k8s returns immediately without touching the client."""
    h = _install_fake_k8s(monkeypatch)
    h["config"].load_incluster_config.side_effect = RuntimeError("no kubeconfig")
    store = CooldownStore(redis_url=None)
    await store.connect()
    assert store._k8s_available is False

    store._memory["k"] = time.monotonic() + 5
    h["v1"].patch_namespaced_config_map.reset_mock()

    store._sync_to_k8s()  # guarded by `if not self._k8s_available: return`
    h["v1"].patch_namespaced_config_map.assert_not_called()
