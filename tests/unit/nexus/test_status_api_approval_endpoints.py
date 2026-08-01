"""Unit tests for the human-approval endpoints in nexus.observability.status_api.

Covers the rename (human_queue → approval_queue) plus the two audit-trailing
behaviors added in the self-healing plan:
  - POST /approve/{action_id} approves and records to the AuditTrail
  - POST /reject/{action_id}  rejects  and records to the AuditTrail
  - GET  /approvals/pending   lists the queue
  - 404 when the action id is unknown / 503 when the ladder isn't accessible

Endpoints are plain async functions reading the module-level ``context``;
we set context fields directly (and restore them in teardown), matching
test_status_api_nexus_context.
"""

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from nexus.observability import status_api as status_api_module


@pytest.fixture
def fresh_context():
    """Hand each test a module-level context with every dep field nulled.

    ``context`` is a process-global singleton shared by every test file, and
    other tests may leave it populated. Rather than assume an upstream clean
    baseline (which breaks under cross-file ordering), we null every NexusContext
    dep field before yield and restore the prior snapshot after — making these
    tests order-independent regardless of upstream leakage.
    """
    ctx = status_api_module.context
    dep_fields = [f.name for f in dataclasses.fields(ctx) if f.name != "started_at"]
    saved = {f: getattr(ctx, f) for f in dep_fields}
    for f in dep_fields:
        setattr(ctx, f, None)  # clean baseline
    yield ctx
    for f, v in saved.items():
        setattr(ctx, f, v)


def _ladder_with_queue(approve_ids=(), reject_ids=(), pending=()):
    """Build a real-ish ladder object exposing approval_queue + pending_list."""
    queue = MagicMock()
    queue.approve = MagicMock(side_effect=lambda aid: aid in approve_ids)
    queue.reject = MagicMock(side_effect=lambda aid: aid in reject_ids)
    queue.pending_list = MagicMock(return_value=list(pending))
    ladder = MagicMock()
    ladder.approval_queue = queue
    return ladder, queue


def _orchestrator(ladder):
    orc = MagicMock()
    orc.executor.ladder = ladder
    return orc


# ── /approve/{action_id} ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_approves_and_records_to_audit_trail(fresh_context):
    ladder, queue = _ladder_with_queue(approve_ids={"ABC12345"})
    fresh_context.orchestrator = _orchestrator(ladder)
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    resp = await status_api_module.approve_action("ABC12345")

    assert resp == {"status": "approved", "action_id": "ABC12345"}
    queue.approve.assert_called_once_with("ABC12345")
    fresh_context.audit_trail.record_approval.assert_awaited_once_with(
        "ABC12345", "api_user"
    )


@pytest.mark.asyncio
async def test_approve_404_when_id_unknown(fresh_context):
    ladder, _ = _ladder_with_queue(approve_ids=set())  # nothing approvable
    fresh_context.orchestrator = _orchestrator(ladder)
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.approve_action("NOPE1234")
    assert exc_info.value.status_code == 404
    fresh_context.audit_trail.record_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_skips_audit_when_no_audit_trail(fresh_context):
    """No audit trail configured → approve still succeeds, just doesn't record."""
    ladder, _ = _ladder_with_queue(approve_ids={"OK000001"})
    fresh_context.orchestrator = _orchestrator(ladder)
    # fresh_context.audit_trail left None

    resp = await status_api_module.approve_action("OK000001")
    assert resp == {"status": "approved", "action_id": "OK000001"}


@pytest.mark.asyncio
async def test_approve_503_when_no_orchestrator(fresh_context):
    # orchestrator left None
    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.approve_action("ANY12345")
    assert exc_info.value.status_code == 503


# ── /reject/{action_id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_rejects_and_records_to_audit_trail(fresh_context):
    ladder, queue = _ladder_with_queue(reject_ids={"REJ00002"})
    fresh_context.orchestrator = _orchestrator(ladder)
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_rejection = AsyncMock()

    resp = await status_api_module.reject_action("REJ00002")

    assert resp == {"status": "rejected", "action_id": "REJ00002"}
    queue.reject.assert_called_once_with("REJ00002")
    fresh_context.audit_trail.record_rejection.assert_awaited_once_with(
        "REJ00002", "api_user"
    )


@pytest.mark.asyncio
async def test_reject_404_when_id_unknown(fresh_context):
    ladder, _ = _ladder_with_queue(reject_ids=set())
    fresh_context.orchestrator = _orchestrator(ladder)
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_rejection = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.reject_action("NOPE0000")
    assert exc_info.value.status_code == 404
    fresh_context.audit_trail.record_rejection.assert_not_awaited()


# ── /approvals/pending ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_approvals_returns_queue_list(fresh_context):
    from nexus.governance.action_ladder import PendingApproval

    p1 = PendingApproval(
        approval_id="A1", runbook_id="llm_dynamic",
        action_type="restart_deployment", target="shop-demo",
        incident_id="CL-1", healing_level=2, confidence=0.8,
        enqueued_at="2026-07-31T07:04:27.285226+00:00", context={"proposed_tool": "x"},
    )
    ladder, _ = _ladder_with_queue(pending=[p1])
    fresh_context.orchestrator = _orchestrator(ladder)

    out = await status_api_module.pending_approvals()
    # Endpoint serializes PendingApproval → dict so the list[dict] response
    # model validates under pydantic v2 (was 500ing on dataclass-as-dict).
    assert out == [p1.to_dict()]
    assert out[0]["approval_id"] == "A1"


@pytest.mark.asyncio
async def test_pending_approvals_empty_when_ladder_missing(fresh_context):
    """If approval_queue is missing on the ladder, pending_approvals degrades to []."""
    # spec-limit ladder to only `human_queue` — accessing `.approval_queue` then
    # raises AttributeError, mirroring the original bug the rename fixed.
    ladder = MagicMock(spec=["human_queue"])
    orc = MagicMock()
    orc.executor.ladder = ladder
    fresh_context.orchestrator = orc

    out = await status_api_module.pending_approvals()
    assert out == []
