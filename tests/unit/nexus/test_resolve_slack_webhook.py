"""Tests for the single-source Slack webhook resolver.

``resolve_slack_webhook`` is the one place the governed Notifier path and
chatops.send_approval_request both ask "which webhook URL?". The contract:

  - app not loaded in policy cache → global SLACK_WEBHOOK env fallback
    (this is the fix for deployments that don't mount a selfheal.yaml)
  - app IS loaded → its per-app slack_webhook is authoritative, even when it
    evaluates to None (a disabled app stays silent over the global default;
    the env is NOT resurrected for a pinned app)
  - neither resolves → None (caller silently skips)

Pre-existing behavior: the governed Notifier read only from the policy cache,
so a deployment with no mounted selfheal.yaml sent nothing even with
SLACK_WEBHOOK set. chatops read SLACK_WEBHOOK_URL directly. Both now share one
resolver with the env fallback, so a single SLACK_WEBHOOK var works everywhere.
"""

from __future__ import annotations

import pytest

from nexus.integration.notifier import resolve_slack_webhook


@pytest.fixture
def env_webhook(monkeypatch):
    """The global fallback webhook present in the environment."""
    monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.slack.test/GLOBAL")
    return "https://hooks.slack.test/GLOBAL"


@pytest.fixture
def empty_policy_cache(monkeypatch):
    """No app policies loaded → forces the env-fallback branch."""
    cache: dict[str, dict] = {}
    monkeypatch.setattr("nexus.integration.dashboard._policy_cache", cache)
    return cache


# ── env fallback ─────────────────────────────────────────────────────────────


def test_env_fallback_used_when_app_not_in_cache(env_webhook, empty_policy_cache):
    assert resolve_slack_webhook("nexus") == env_webhook
    assert resolve_slack_webhook("shop-demo") == env_webhook


def test_env_fallback_used_when_app_name_is_none(env_webhook, empty_policy_cache):
    assert resolve_slack_webhook(None) == env_webhook


def test_returns_none_when_neither_env_nor_cache(monkeypatch, empty_policy_cache):
    monkeypatch.delenv("SLACK_WEBHOOK", raising=False)
    for app in ("nexus", "shop-demo", None):
        assert resolve_slack_webhook(app) is None


# ── per-app override is authoritative ────────────────────────────────────────


def test_per_app_override_wins_over_env(env_webhook, empty_policy_cache):
    empty_policy_cache["shop-demo"] = {
        "notifications": {"slack_webhook": "https://hooks.slack.test/PERAPP"}
    }
    assert resolve_slack_webhook("shop-demo") == "https://hooks.slack.test/PERAPP"
    # A different app with no policy still gets the env fallback.
    assert resolve_slack_webhook("nexus") == env_webhook


def test_pinned_app_with_null_webhook_stays_silent_over_env(env_webhook, empty_policy_cache):
    """An app loaded in cache but with slack_webhook absent/empty → None.

    The cache entry is authoritative: a policy-pinned app that configured no
    webhook must not be resurrected by the global env default.
    """
    empty_policy_cache["shop-demo"] = {"notifications": {}}
    assert resolve_slack_webhook("shop-demo") is None
    empty_policy_cache["other"] = {"notifications": {"slack_webhook": ""}}
    assert resolve_slack_webhook("other") is None


def test_dashboard_import_failure_falls_back_to_env(env_webhook, monkeypatch):
    """If the policy cache module can't be imported, resolver still returns env."""
    import builtins

    real_import = builtins.__import__

    def _fail(name, *args, **kwargs):
        if name == "nexus.integration.dashboard":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail)
    # No assertion on cache state (we can't reach it); env is the only path.
    assert resolve_slack_webhook("anything") == env_webhook


# ── ponytail: self-checks that pin the load-bearing resolution order ─────────


def test_resolution_order_env_then_cache_wins_in_order(env_webhook, empty_policy_cache):
    """Pin: cache hit short-circuits BEFORE the env read (authoritative-first).

    If a future refactor swaps the order, the env fallback would mask a pinned
    disabled app — this is the regression we're guarding against.
    """
    empty_policy_cache["shop-demo"] = {"notifications": {"slack_webhook": "PERAPP"}}
    # app in cache → PERAPP, not env
    assert resolve_slack_webhook("shop-demo") == "PERAPP"
    # delete it → falls to env
    empty_policy_cache.pop("shop-demo")
    assert resolve_slack_webhook("shop-demo") == env_webhook
