"""Unit tests for the human-approval endpoints in nexus.observability.status_api.

P0 (governed re-dispatch) changed the contract: an approved action no longer
just records to the AuditTrail and returns — it is re-dispatched through
RunbookExecutor.execute_approved(), the single point where a human-approved
action touches the cluster under the full governance plane (ladder, cooldown,
circuit breaker, audit, rollback).

Covered:
  - POST /approve/{action_id}  approves, records to AuditTrail, dispatches via execute_approved
  - POST /approve/{action_id}  404 when the action id is unknown
  - POST /approve/{action_id}  idempotent on repeat (already_approved)
  - POST /approve/{action_id}  503 when the orchestrator/executor is missing
  - POST /reject/{action_id}   rejects and records to AuditTrail (unchanged)
  - POST /reject/{action_id}   404 when id unknown
  - GET  /approvals/pending     lists the queue
  - execute_approved (integration): a human-approved LLM proposal is BLOCKED
    when the governance circuit breaker is open, and SUCCEEDS (dry-run) when
    the breaker is closed — proving the routed path passes the ladder.

Endpoint tests read the module-level ``context``; we set fields directly and
restore them in teardown (matching test_status_api_nexus_context).
"""

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from nexus.governance.action_ladder import (
    ActionLadder,
    GovernanceCircuitBreaker,
    HumanApprovalQueue,
    PendingApproval,
)
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.policy_engine import PolicyEngine
from nexus.governance.rollback_registry import RollbackRegistry
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.observability import status_api as status_api_module


@pytest.fixture
def fresh_context():
    """Hand each test a module-level context with every dep field nulled.

    ``context`` is a process-global singleton shared by every test file, and
    other tests may leave it populated. We null every NexusContext dep field
    before yield and restore the prior snapshot after — order-independent.
    """
    ctx = status_api_module.context
    dep_fields = [f.name for f in dataclasses.fields(ctx) if f.name != "started_at"]
    saved = {f: getattr(ctx, f) for f in dep_fields}
    for f in dep_fields:
        setattr(ctx, f, None)  # clean baseline
    yield ctx
    for f, v in saved.items():
        setattr(ctx, f, v)


def _executor_with_real_queue(approved_ids=(), staged=()):
    """Build a mock-backed executor whose approval_queue is a REAL queue.

    Using a real HumanApprovalQueue fixes the pre-existing mock artifact where
    ``is_approved``/``is_rejected`` returned a truthy MagicMock and short
    -circuited the idempotency checks. execute_approved is mocked so the
    endpoint test asserts dispatch wiring, not k8s behavior (that lives in the
    integration tests below).
    """
    queue = HumanApprovalQueue()  # no NATS → enqueue is sync-safe
    for p in staged:
        queue._pending[p.approval_id] = p
    for aid in approved_ids:
        queue._approved.add(aid)
    executor = MagicMock()
    executor.ladder.approval_queue = queue
    executor.execute_approved = AsyncMock(
        return_value={"status": "success", "action_id": "audit-1", "result": {}}
    )
    orc = MagicMock()
    orc.executor = executor
    return orc, queue, executor


def _pending(approval_id, runbook_id="runbook_db_v1", action_type="restart_pod"):
    return PendingApproval(
        approval_id=approval_id,
        runbook_id=runbook_id,
        action_type=action_type,
        target="default/db",
        incident_id="CL-1",
        healing_level=3,
        confidence=0.7,
        enqueued_at="2026-08-01T00:00:00+00:00",
        context={"event_id": "e1"},
    )


# ── /approve/{action_id} ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_dispatches_through_execute_approved(fresh_context):
    p = _pending("ABC12345")
    orc, queue, executor = _executor_with_real_queue(staged=[p])
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    resp = await status_api_module.approve_action("ABC12345")

    assert resp["status"] == "approved"
    assert resp["action_id"] == "ABC12345"
    assert resp["outcome"]["status"] == "success"  # from execute_approved mock
    assert queue.is_approved("ABC12345")
    fresh_context.audit_trail.record_approval.assert_awaited_once_with(
        "ABC12345", "api_user"
    )
    # The dispatch must receive the staged pending item (not an id string).
    executor.execute_approved.assert_awaited_once()
    dispatched = executor.execute_approved.await_args.args[0]
    assert isinstance(dispatched, PendingApproval)
    assert dispatched.approval_id == "ABC12345"


@pytest.mark.asyncio
async def test_approve_404_when_id_unknown(fresh_context):
    orc, _, _ = _executor_with_real_queue()  # empty queue
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.approve_action("NOPE1234")
    assert exc_info.value.status_code == 404
    fresh_context.audit_trail.record_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_idempotent_when_already_approved(fresh_context):
    p = _pending("OK000001")
    orc, _, executor = _executor_with_real_queue(approved_ids=["OK000001"], staged=[p])
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    resp = await status_api_module.approve_action("OK000001")
    assert resp == {"status": "already_approved", "action_id": "OK000001"}
    # Must NOT re-dispatch (that's the whole point of idempotency).
    executor.execute_approved.assert_not_awaited()
    fresh_context.audit_trail.record_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_skips_dispatch_when_staging_lost(fresh_context):
    """If the pending item was evicted before approve (e.g. clear_resolved ran),
    approve still records but cannot re-dispatch — degrade gracefully."""
    orc, _, executor = _executor_with_real_queue(staged=[])
    # Patch approve to True so we pass the 404 gate, simulating a just-evicted
    # item whose approve() succeeds but pending_list() no longer has it.
    orc.executor.ladder.approval_queue.approve = lambda aid: True
    orc.executor.ladder.approval_queue.is_approved = lambda aid: False
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_approval = AsyncMock()

    resp = await status_api_module.approve_action("GHOST001")
    assert resp["status"] == "approved"
    assert resp["action_id"] == "GHOST001"
    executor.execute_approved.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_503_when_no_orchestrator(fresh_context):
    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.approve_action("ANY12345")
    assert exc_info.value.status_code == 503


# ── /reject/{action_id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_rejects_and_records_to_audit_trail(fresh_context):
    p = _pending("REJ00002")
    orc, queue, executor = _executor_with_real_queue(staged=[p])
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_rejection = AsyncMock()

    resp = await status_api_module.reject_action("REJ00002")

    assert resp == {"status": "rejected", "action_id": "REJ00002"}
    assert queue.is_rejected("REJ00002")
    fresh_context.audit_trail.record_rejection.assert_awaited_once_with(
        "REJ00002", "api_user"
    )


@pytest.mark.asyncio
async def test_reject_404_when_id_unknown(fresh_context):
    orc, _, _ = _executor_with_real_queue()
    fresh_context.orchestrator = orc
    fresh_context.audit_trail = MagicMock()
    fresh_context.audit_trail.record_rejection = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await status_api_module.reject_action("NOPE0000")
    assert exc_info.value.status_code == 404
    fresh_context.audit_trail.record_rejection.assert_not_awaited()


# ── /approvals/pending ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_approvals_returns_queue_list(fresh_context):
    p1 = PendingApproval(
        approval_id="A1", runbook_id="llm_dynamic",
        action_type="restart_deployment", target="shop-demo",
        incident_id="CL-1", healing_level=2, confidence=0.8,
        enqueued_at="2026-07-31T07:04:27.285226+00:00", context={"proposed_tool": "x"},
    )
    orc, _, _ = _executor_with_real_queue(staged=[p1])
    fresh_context.orchestrator = orc

    out = await status_api_module.pending_approvals()
    assert out == [p1.to_dict()]
    assert out[0]["approval_id"] == "A1"


@pytest.mark.asyncio
async def test_pending_approvals_empty_when_ladder_missing(fresh_context):
    ladder = MagicMock(spec=["human_queue"])  # no approval_queue attr
    orc = MagicMock()
    orc.executor.ladder = ladder
    fresh_context.orchestrator = orc

    out = await status_api_module.pending_approvals()
    assert out == []


# ── execute_approved integration (the load-bearing P0 path) ────────────────────


def _llm_pending(action_id="LLM12345"):
    """A staged llm_dynamic approval as handle_cluster() would build it,
    staged at restart_deployment's real level (L1) with a scored confidence."""
    return PendingApproval(
        approval_id=action_id,
        runbook_id="llm_dynamic",
        action_type="restart_deployment",
        target="default/payments",
        incident_id="CL-PAY",
        healing_level=1,
        confidence=0.72,
        enqueued_at="2026-08-01T00:00:00+00:00",
        context={
            "proposed_tool": "restart_deployment",
            "tool_args": {"namespace": "default", "deployment_name": "payments"},
            "cluster_summary": {
                "cluster_id": "CL-PAY",
                "namespace": "default",
                "primary_resource": "payments",
            },
            "llm_diagnosis": "restart to clear bad pod",
            "tool_level": 1,
            "blast_radius": "single_deployment",
            "effective_level": 1,
        },
    )


def _real_executor(tmp_path, cb_open=False):
    """Real ladder + policy fallback + cooldown + dry-run executor (no k8s).

    The whole point: an approved LLM action reaches the REAL ActionLadder, not
    a mock, so its gates actually fire. dry_run keeps _execute_action off the
    kube API; _ensure_k8s is stubbed so no cluster is needed.
    """
    cb = GovernanceCircuitBreaker(failure_threshold=3)
    if cb_open:
        cb._state = GovernanceCircuitBreaker.OPEN  # force tripped
    ladder = ActionLadder(
        PolicyEngine(opa_url="http://localhost:1"),  # unreachable → fallback
        CooldownStore(),
        HumanApprovalQueue(),
        cb,
    )
    audit = MagicMock()
    audit.write_pending = AsyncMock(return_value="audit-1")
    audit.update_outcome = AsyncMock()
    executor = RunbookExecutor(
        nats_client=None,
        audit_trail=audit,
        action_ladder=ladder,
        rollback_registry=RollbackRegistry(dry_run=True),
        runbook_dir=tmp_path,  # no runbooks loaded — LLM path doesn't need them
        dry_run=True,
        confidence=0.8,
    )
    executor._ensure_k8s = lambda: None  # no cluster in unit tests
    return executor, audit, ladder


@pytest.mark.asyncio
async def test_execute_approved_llm_runs_through_governance_when_cb_closed(tmp_path):
    executor, audit, ladder = _real_executor(tmp_path, cb_open=False)
    pending = _llm_pending()

    outcome = await executor.execute_approved(pending)

    assert outcome["status"] == "success"
    assert outcome["action_id"] == "audit-1"
    # Audit written before execution, outcome updated after.
    audit.write_pending.assert_awaited_once()
    assert audit.write_pending.await_args.kwargs["triggered_by"] == "human:api_user"
    assert audit.write_pending.await_args.kwargs["runbook_id"] == "llm_dynamic"
    audit.update_outcome.assert_awaited_once()
    # Success path resets the breaker and sets cooldown.
    assert ladder.governance_cb.state == GovernanceCircuitBreaker.CLOSED


@pytest.mark.asyncio
async def test_execute_approved_blocked_when_circuit_breaker_open(tmp_path):
    """A human can approve, but governance still blocks if the breaker is open —
    the human may not have known autonomous healing had already failed 3×."""
    executor, audit, ladder = _real_executor(tmp_path, cb_open=True)
    pending = _llm_pending()

    outcome = await executor.execute_approved(pending)

    assert outcome["status"] == "governance_blocked"
    assert "governance_cb_open" in outcome["reason"]
    # Blocked BEFORE execution: no audit pending row, no outcome update.
    audit.write_pending.assert_not_awaited()
    audit.update_outcome.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_approved_rulebased_requires_staged_snapshot(tmp_path):
    """A rule-based approval without the staged action/event snapshot can't be
    re-dispatched — we stage those snapshots in ActionLadder.evaluate now."""
    executor, _, _ = _real_executor(tmp_path, cb_open=False)
    pending = _pending("RB000001", runbook_id="runbook_db_v1", action_type="restart_pod")
    # No 'action'/'event' keys in context → materialize should raise ValueError.
    with pytest.raises(ValueError, match="missing staged event/action snapshot"):
        await executor.execute_approved(pending)

@pytest.mark.asyncio
async def test_execute_approved_manual_review_does_not_dispatch(tmp_path):
    """manual_review = the LLM staged a diagnosis for a human to read, not a
    concrete remediation (both orchestrator.py and llm_orchestrator.py enqueue
    it when no mapped runbook / no function call came back). There is no cluster
    action to run, so approving it must NOT touch k8s or the ladder — it records
    an audit row and resolves, rather than crashing on the unmapped tool name.
    """
    executor, audit, ladder = _real_executor(tmp_path, cb_open=False)
    pending = PendingApproval(
        approval_id="MR000001",
        runbook_id="llm_dynamic",
        action_type="manual_review",
        target="monitoring/prometheus-0",
        incident_id="CL-503",
        healing_level=0,
        confidence=0.56,
        enqueued_at="2026-08-01T00:00:00+00:00",
        context={"cluster_summary": {"cluster_id": "CL-503"}, "llm_diagnosis": "drain"},
    )

    outcome = await executor.execute_approved(pending)

    assert outcome["status"] == "reviewed"
    assert outcome["action_id"] == "audit-1"
    assert outcome["result"]["action"] == "manual_review"
    # Review is audited (one pending row + outcome) so the dashboard shows it...
    audit.write_pending.assert_awaited_once()
    assert audit.write_pending.await_args.kwargs["healing_level"] == 0
    audit.update_outcome.assert_awaited_once()
    # ...but the circuit breaker/cooldown are untouched — no action hit the cluster.
    assert ladder.governance_cb.consecutive_failures == 0
    # No k8s client was ever needed.
    assert not hasattr(executor, "_k8s_apps") or getattr(executor, "_k8s_apps", None) is None


@pytest.mark.asyncio
async def test_execute_approved_manual_review_via_slack_path(tmp_path, monkeypatch):
    """The Slack /slack/interactive approve path routes the same manual_review
    item through execute_approved and must surface the reviewed outcome, not
    raise 'unknown LLM tool'.

    Contract after the fast-ack change: the synchronous response is a ⏸
    placeholder (so Slack's ~3s interactive window is never breached by the
    dispatch); the real outcome is POSTed to Slack's response_url by a
    background task. So: assert the sync placeholder, then assert the
    response_url replace carries the reviewed outcome.
    """
    import hashlib
    import hmac
    import json as _json
    import time as _time
    from unittest.mock import AsyncMock as _AsyncMock
    from urllib.parse import urlencode

    import httpx
    from fastapi import FastAPI
    from httpx import ASGITransport

    from nexus.integration import notifier as notifier_module

    monkeypatch.setattr(notifier_module, "SLACK_SIGNING_SECRET", "test-signing-secret")
    # Capture the response_url replace the background task POSTs (so we don't
    # hit Slack or depend on a live URL) and assert it carries the outcome.
    url_replacer = _AsyncMock()
    monkeypatch.setattr(status_api_module, "_slack_replace_via_response_url", url_replacer)

    executor, _, _ = _real_executor(tmp_path, cb_open=False)
    pending = PendingApproval(
        approval_id="BE57C491",
        runbook_id="llm_dynamic",
        action_type="manual_review",
        target="monitoring/prometheus-0",
        incident_id="CL-PROM",
        healing_level=0,
        confidence=0.56,
        enqueued_at="2026-08-01T00:00:00+00:00",
        context={"cluster_summary": {}, "llm_diagnosis": "resource_exhaustion"},
    )
    # Stage into the ladder's own queue (approval_queue is a read-only property).
    executor.ladder.approval_queue._pending[pending.approval_id] = pending
    orc = MagicMock()
    orc.executor = executor

    ctx = status_api_module.context
    saved_orc = ctx.orchestrator
    saved_audit = ctx.audit_trail
    ctx.orchestrator = orc
    # The HTTP handler awaits context.audit_trail.record_approval (the human's
    # approve record); give it an async mock — separate from the executor's
    # audit (audit write/update during dispatch), matching how the other
    # endpoint tests wire fresh_context.audit_trail.
    ctx.audit_trail = MagicMock()
    ctx.audit_trail.record_approval = AsyncMock()
    try:
        app = FastAPI()
        app.post("/slack/interactive")(status_api_module.slack_interactive)

        body = urlencode({"payload": _json.dumps({
            "type": "block_actions",
            "user": {"id": "U9", "username": "vatsal"},
            "actions": [{"action_id": "nexus_approve", "value": "BE57C491"}],
            "response_url": "https://hooks.slack.test/T/B",
        })}).encode()
        ts = str(int(_time.time()))
        sig = "v0=" + hmac.new(b"test-signing-secret",
                               b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/slack/interactive", content=body, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            })
        # Synchronous response: the ⏸ placeholder, not the outcome — the card's
        # buttons vanish immediately so Slack never keeps an un-acked click alive.
        assert resp.status_code == 200
        sync_body = resp.json()
        assert sync_body["replace_original"] is True
        assert sync_body["text"].startswith("⏸")
        assert executor.ladder.approval_queue.is_approved("BE57C491")
        # Background: the reviewed outcome was POSTed to the response_url, not to
        # the synchronous body. ASGITransport drives background tasks to
        # completion within the request, so the replacer has run by now.
        url_replacer.assert_awaited_once()
        final_url, final_text = url_replacer.await_args.args
        assert final_url == "https://hooks.slack.test/T/B"
        assert "BE57C491" in final_text and "reviewed" in final_text
    finally:
        ctx.orchestrator = saved_orc
        ctx.audit_trail = saved_audit
