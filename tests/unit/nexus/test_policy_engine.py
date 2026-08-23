"""Unit tests for nexus.governance.policy_engine — OPA-only, fail-closed.

OPA is a hard dependency: any failure talking to it (unreachable, HTTP error,
malformed body) must DENY with source="error" — never allow, never fall back
to Python logic.

Also guards deploy wiring: the rego embedded in the nexus-opa ConfigMap must
match deploy/opa/nexus_policies.rego, since the cluster runs the embedded copy.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from nexus.governance import policy_engine as pe_module
from nexus.governance.policy_engine import PolicyEngine


def _patch_opa(monkeypatch, handler) -> list[httpx.Request]:
    """Route every PolicyEngine HTTP call through MockTransport(handler)."""
    calls: list[httpx.Request] = []

    def tracking_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    # Capture the real client BEFORE patching — pe_module.httpx is the shared
    # module, so calling httpx.AsyncClient inside make_client would recurse.
    original_client = pe_module.httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(tracking_handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(pe_module.httpx, "AsyncClient", make_client)
    return calls


# ── evaluate() ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allow_passthrough(monkeypatch):
    """OPA says allow → decision passed through with source='opa'."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("allow_action"):
            return httpx.Response(200, json={"result": True})
        if path.endswith("deny_reasons"):
            return httpx.Response(200, json={"result": []})
        return httpx.Response(200, json={"result": False})

    _patch_opa(monkeypatch, handler)
    d = await PolicyEngine().evaluate("restart_pod", 1)
    assert d.allowed
    assert d.source == "opa"
    assert d.requires_approval is False
    assert d.deny_reasons == []


@pytest.mark.asyncio
async def test_deny_reasons_and_approval_passthrough(monkeypatch):
    """Deny + reasons + requires_human_approval all flow through from OPA."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("allow_action"):
            return httpx.Response(200, json={"result": False})
        if path.endswith("deny_reasons"):
            return httpx.Response(200, json={"result": ["action_in_cooldown"]})
        return httpx.Response(200, json={"result": True})

    _patch_opa(monkeypatch, handler)
    d = await PolicyEngine().evaluate("scale_deployment", 2, in_cooldown=True)
    assert d.denied
    assert d.requires_approval is True
    assert d.deny_reasons == ["action_in_cooldown"]
    assert d.source == "opa"


@pytest.mark.asyncio
async def test_unreachable_fail_closed_and_retry_window(monkeypatch):
    """OPA unreachable → DENY source='error'; no retry inside the 60s window."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    calls = _patch_opa(monkeypatch, handler)
    engine = PolicyEngine()

    d = await engine.evaluate("restart_pod", 1)
    assert d.denied
    assert d.source == "error"
    assert "opa_unavailable_deny_closed" in d.deny_reasons
    assert engine.opa_available is False

    # Within the retry cooldown window no further network attempts are made.
    assert len(calls) == 1
    d2 = await engine.evaluate("restart_pod", 1)
    assert d2.denied and d2.source == "error"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_error_fail_closed(monkeypatch):
    """HTTP 500 from OPA → fail-closed deny, not a passthrough."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("allow_action"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"result": True})

    _patch_opa(monkeypatch, handler)
    d = await PolicyEngine().evaluate("drain_node", 3)
    assert d.denied and d.source == "error"


@pytest.mark.asyncio
async def test_malformed_body_fail_closed(monkeypatch):
    """A non-JSON body must never parse into an implicit ALLOW."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    _patch_opa(monkeypatch, handler)
    d = await PolicyEngine().evaluate("restart_pod", 1)
    assert d.denied and d.source == "error"


# ── is_healthy() ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_healthy(monkeypatch):
    healthy = {"up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not healthy["up"]:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/health":
            return httpx.Response(200)
        return httpx.Response(404)

    # One patch for both phases — a second _patch_opa would wrap the first
    # mock as its "original" and route traffic back to the healthy handler.
    _patch_opa(monkeypatch, handler)
    engine = PolicyEngine()

    assert await engine.is_healthy() is True
    healthy["up"] = False
    assert await engine.is_healthy() is False


# ── Deploy wiring: embedded rego must match the source of truth ──────────────


def test_embedded_rego_matches_source():
    """The rego shipped in the nexus-opa ConfigMap must byte-match
    deploy/opa/nexus_policies.rego — that embedded copy is what runs."""
    root = Path(__file__).resolve().parents[3]
    disk = (root / "deploy/opa/nexus_policies.rego").read_text()
    manifest = root / "deploy/nexus/opa-deployment.yaml"
    docs = list(yaml.safe_load_all(manifest.read_text()))
    cm = next(d for d in docs if isinstance(d, dict) and d.get("kind") == "ConfigMap")
    # |- strips the trailing newline; compare content only.
    assert cm["data"]["nexus_policies.rego"].rstrip("\n") == disk.rstrip("\n")
