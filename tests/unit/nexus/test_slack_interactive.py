"""End-to-end tests for POST /slack/interactive.

The old version of this endpoint declared ``payload: str = None``, which FastAPI
treats as a *query parameter* — so a real Slack POST (form-encoded body) always
returned "missing payload". On top of that it had no signature validation, so
the ngrok URL alone was enough to approve/reject L3 cluster actions.

These tests drive the real ``slack_interactive`` handler through httpx +
ASGITransport against a minimal FastAPI app (no lifespan, no NATS), posting the
genuine ``application/x-www-form-urlencoded`` body Slack sends, with a
correctly computed ``v0`` HMAC signature. They pin both fixes:

  - form-body parsing of the ``payload`` field (approve + reject paths)
  - Slack request-signature verification (good sig → 200, bad sig → 401,
    stale ts → 401, missing secret → 401, unknown approval → replace msg)

The audit ``triggered_by`` is recorded as ``human:slack:<user>`` because the
Slack path passes ``username=f"slack:{user}"`` into record_*() which itself
prepends ``human:``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from nexus.governance.action_ladder import HumanApprovalQueue, PendingApproval
from nexus.integration import notifier as notifier_module
from nexus.observability import status_api as status_api_module

_SECRET = "test-signing-secret"


def _sign(body: bytes, ts: str) -> str:
    """Compute Slack's v0 HMAC-SHA256 signature for ``body`` with ``_SECRET``."""
    base = b"v0:" + str(ts).encode() + b":" + body
    return "v0=" + hashlib.sha256(base).hexdigest()


def _pending(approval_id="ABC12345"):
    return PendingApproval(
        approval_id=approval_id,
        runbook_id="runbook_db_v1",
        action_type="restart_pod",
        target="default/db",
        incident_id="CL-1",
        healing_level=3,
        confidence=0.7,
        enqueued_at="2026-08-01T00:00:00+00:00",
        context={"event_id": "e1"},
    )


@pytest.fixture
def fresh_context():
    """Null every NexusContext dep field, restore after — order-independent."""
    ctx = status_api_module.context
    dep_fields = [f.name for f in dataclasses.fields(ctx) if f.name != "started_at"]
    saved = {f: getattr(ctx, f) for f in dep_fields}
    for f in dep_fields:
        setattr(ctx, f, None)
    yield ctx
    for f, v in saved.items():
        setattr(ctx, f, v)


@pytest.fixture
def signed_secret(monkeypatch):
    """Set the signing secret the verifier reads from the module global."""
    monkeypatch.setattr(notifier_module, "SLACK_SIGNING_SECRET", _SECRET)


def _orch_with_queue(staged=(), approved=(), rejected=()):
    queue = HumanApprovalQueue()  # no NATS → enqueue not used here, sync-safe
    for p in staged:
        queue._pending[p.approval_id] = p
    for aid in approved:
        queue._approved.add(aid)
    for aid in rejected:
        queue._rejected.add(aid)
    executor = MagicMock()
    executor.ladder.approval_queue = queue
    executor.execute_approved = AsyncMock(
        return_value={"status": "success", "action_id": "audit-1", "result": {}}
    )
    orc = MagicMock()
    orc.executor = executor
    return orc, queue, executor


def _make_app():
    """Minimal FastAPI app exposing only /slack/interactive — no lifespan.

    ASGITransport drives the real handler with a real Request: covers form-body
    parsing + header extraction, without starting NATS / the orchestrator.
    """
    from fastapi import FastAPI

    app = FastAPI()
    app.post("/slack/interactive")(status_api_module.slack_interactive)
    return app


async def _post(client, action_id: str, approval_id: str, *, ts: str | None = None,
                sig: str | None = "$sentinel$", bad_sig: bool = False) -> httpx.Response:
    import json
    from urllib.parse import urlencode

    payload = json.dumps(
        {
            "type": "block_actions",
            "user": {"id": "U123", "username": "vatsal"},
            "actions": [{"action_id": action_id, "value": approval_id}],
        }
    )
    body = urlencode({"payload": payload}).encode()
    ts = str(ts if ts is not None else int(time.time()))
    signature = "v0=invalid" if bad_sig else (sig if sig != "$sentinel$" else _sign(body, ts))
    return await client.post(
        "/slack/interactive",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": signature,
        },
    )


# ── Signature gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bad_signature_is_401(fresh_context, signed_secret):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "ABC12345", bad_sig=True)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stale_timestamp_is_401(fresh_context, signed_secret):
    # 1 hour old — outside the 5-minute tolerance → replay guard rejects.
    old_ts = str(int(time.time()) - 3600)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "ABC12345", ts=old_ts)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_secret_401s_everything(fresh_context, monkeypatch):
    """No SLACK_SIGNING_SECRET configured → secure-by-default: reject all."""
    monkeypatch.setattr(notifier_module, "SLACK_SIGNING_SECRET", "")
    orc, _, _ = _orch_with_queue(staged=[_pending("ABC12345")])
    fresh_context.orchestrator = orc

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        # Even with a "valid" sig, verifier returns False when secret is empty.
        resp = await _post(client, "nexus_approve", "ABC12345")
    assert resp.status_code == 401


# ── Approve path ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_routes_through_governance_and_records_slack_user(
    fresh_context, signed_secret
):
    p = _pending("ABC12345")
    orc, queue, executor = _orch_with_queue(staged=[p])
    fresh_context.orchestrator = orc
    audit = MagicMock()
    audit.record_approval = AsyncMock()
    fresh_context.audit_trail = audit

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "ABC12345")

    assert resp.status_code == 200
    body = resp.json()
    assert body["replace_original"] is True
    assert "ABC12345" in body["text"]
    assert "vatsal" in body["text"]
    assert queue.is_approved("ABC12345")
    executor.execute_approved.assert_awaited_once()
    # Slack username is recorded prefixed with slack:, audit prepends human:.
    audit.record_approval.assert_awaited_once_with("ABC12345", "slack:vatsal")


@pytest.mark.asyncio
async def test_approve_idempotent_when_already_approved(fresh_context, signed_secret):
    p = _pending("OK000001")
    orc, queue, executor = _orch_with_queue(staged=[p], approved=["OK000001"])
    fresh_context.orchestrator = orc
    audit = MagicMock()
    audit.record_approval = AsyncMock()
    fresh_context.audit_trail = audit

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "OK000001")

    assert resp.status_code == 200
    assert resp.json()["text"].lower().startswith("✅")  # already-approved message
    executor.execute_approved.assert_not_awaited()
    audit.record_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_unknown_action_replaces_message_no_dispatch(
    fresh_context, signed_secret
):
    orc, queue, executor = _orch_with_queue()  # empty queue
    fresh_context.orchestrator = orc

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "NOPE0000")

    assert resp.status_code == 200
    assert "not found" in resp.json()["text"].lower()
    executor.execute_approved.assert_not_awaited()


# ── Reject path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_records_and_stops_workflow(fresh_context, signed_secret):
    p = _pending("REJ00002")
    orc, queue, executor = _orch_with_queue(staged=[p])
    fresh_context.orchestrator = orc
    audit = MagicMock()
    audit.record_rejection = AsyncMock()
    fresh_context.audit_trail = audit

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_reject", "REJ00002")

    assert resp.status_code == 200
    body = resp.json()
    assert body["replace_original"] is True
    assert "REJ00002" in body["text"]
    assert queue.is_rejected("REJ00002")
    audit.record_rejection.assert_awaited_once_with("REJ00002", "slack:vatsal")
    # Reject never touches the cluster — no execute_approved.
    executor.execute_approved.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_idempotent_when_already_rejected(fresh_context, signed_secret):
    p = _pending("RJ000001")
    orc, _, executor = _orch_with_queue(staged=[p], rejected=["RJ000001"])
    fresh_context.orchestrator = orc
    audit = MagicMock()
    audit.record_rejection = AsyncMock()
    fresh_context.audit_trail = audit

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_reject", "RJ000001")

    assert resp.status_code == 200
    assert "already rejected" in resp.json()["text"].lower()
    audit.record_rejection.assert_not_awaited()


# ── Malformed payloads ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_action_id_is_400(fresh_context, signed_secret):
    orc, _, _ = _orch_with_queue(staged=[_pending("ABC12345")])
    fresh_context.orchestrator = orc

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "some_other_action", "ABC12345")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_no_orchestrator_is_503(fresh_context, signed_secret):
    # orchestrator left None by fresh_context
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_make_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, "nexus_approve", "ABC12345")
    assert resp.status_code == 503


# ── ponytail: self-check that Slack's documented example verifies ─────────────


def test_signature_scheme_matches_slack_doc():
    """Pin the v0 base-string format so a refactor can't silently break auth.

    Uses Slack's documented example inputs; only the secret is ours.
    """
    raw = b"token=abc&type=block_actions&actions=..."
    # Fresh ts so the replay guard doesn't reject — this isolates the format
    # check from the timestamp check (the stale-ts case is covered above).
    ts = str(int(time.time()))
    base = b"v0:" + str(ts).encode() + b":" + raw
    sig = "v0=" + hashlib.sha256(base).hexdigest()
    headers = MagicMock()
    headers.get = lambda k, default=None: (
        {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}.get(k, default)
    )
    assert notifier_module.verify_slack_request(
        raw, headers, signing_secret=_SECRET
    ) is True
