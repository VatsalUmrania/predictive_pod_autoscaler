"""Unit tests for nexus.governance.action_ladder.HumanApprovalQueue.

Focus: the new ``nats_client`` wiring in ``enqueue()``. An L3 action staged
into the queue must publish ``nexus.approvals.required`` so the Notifier can
fire the Slack webhook — but only when a NATS client was injected.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.governance.action_ladder import HumanApprovalQueue


def test_enqueue_without_nats_returns_id_and_stages():
    """No NATS client → enqueue still stages the approval (graceful no-op)."""
    queue = HumanApprovalQueue()
    approval_id = queue.enqueue(
        runbook_id="runbook_lambda_error_spike_v1",
        action_type="rollback_alias",
        target="default/auth-lambda",
        incident_id="INC-42",
        healing_level=3,
        confidence=0.6,
        context={"namespace": "default"},
    )

    assert isinstance(approval_id, str)
    assert len(approval_id) == 8  # uppercase 8-char id
    assert approval_id.isupper()
    assert len(queue.pending_list()) == 1


@pytest.mark.asyncio
async def test_enqueue_with_nats_publishes_approval_required_event():
    """With a NATS client injected, enqueue() fires nexus.approvals.required."""
    nats = MagicMock()
    nats.publish_raw = AsyncMock()
    queue = HumanApprovalQueue(nats_client=nats)

    approval_id = queue.enqueue(
        runbook_id="runbook_lambda_error_spike_v1",
        action_type="rollback_alias",
        target="default/auth-lambda",
        incident_id="INC-42",
        healing_level=3,
        confidence=0.6,
        context={"namespace": "payments"},
    )

    # enqueue publishes fire-and-forget via create_task — drain the loop.
    await asyncio.sleep(0.05)

    nats.publish_raw.assert_awaited_once()
    subject, payload = nats.publish_raw.await_args.args
    assert subject == "nexus.approvals.required"
    assert payload["approval_id"] == approval_id
    assert payload["runbook_id"] == "runbook_lambda_error_spike_v1"
    assert payload["healing_level"] == 3
    assert payload["incident_id"] == "INC-42"
    # app is derived from context.namespace so the Notifier routes to Slack
    assert payload["app"] == "payments"


def test_enqueue_without_nats_does_not_attempt_publish():
    """Without a client the publish branch is skipped outright (no AttributeError)."""
    queue = HumanApprovalQueue()
    queue.enqueue(
        runbook_id="rb",
        action_type="act",
        target="t",
        incident_id="i",
        healing_level=3,
        confidence=0.5,
    )
    # Nothing to assert except no exception — the branch is guarded by `if self._nats`.


def test_approve_and_reject_mutate_queue_state():
    """approve()/reject() flag staged ids in their respective resolved sets and
    filter them out of pending_list() (clear_resolved() reaps the dict later)."""
    queue = HumanApprovalQueue()
    first = queue.enqueue("rb", "act", "t1", "i1", 3, 0.5)
    second = queue.enqueue("rb", "act", "t2", "i2", 3, 0.5)

    # Staged id → True; unknown id → False
    assert queue.approve(first) is True
    assert queue.approve("NOPE") is False  # unknown id never staged
    # approve() marks but does NOT remove from _pending (clear_resolved reaps),
    # so re-approving returns True again — this is the real behavior.
    assert queue.is_approved(first) is True
    assert queue.reject(second) is True
    assert queue.reject("NOPE") is False
    assert queue.is_rejected(second) is True

    pending_ids = {p.approval_id for p in queue.pending_list()}
    assert pending_ids == set()  # both filtered out by the approved/rejected sets


def test_pending_list_returns_staged_approval_objects():
    """pending_list() returns one PendingApproval per staged (unresolved) action."""
    queue = HumanApprovalQueue()
    aid = queue.enqueue("runbook_x", "scale", "ns/app", "INC", 2, 0.9, {"k": 1})
    listed = queue.pending_list()
    assert isinstance(listed, list) and len(listed) == 1
    entry = listed[0]  # PendingApproval dataclass — attribute access, not subscript
    assert entry.approval_id == aid
    assert entry.runbook_id == "runbook_x"
    assert entry.healing_level == 2
