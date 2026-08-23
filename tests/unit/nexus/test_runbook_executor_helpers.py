"""Unit tests for RunbookExecutor pure helpers — no k8s / NATS needed.

Covers the pieces of runbook_executor.py that carry branching logic without
touching a cluster: result adaptation, threshold comparison, and LLM-pending
materialization (which maps an approved tool name onto its governance level).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.governance.action_ladder import PendingApproval
from nexus.governance.runbook_executor import RunbookExecutor, _adapt_tool_result


@pytest.fixture
def executor() -> RunbookExecutor:
    return RunbookExecutor(
        nats_client=MagicMock(),
        audit_trail=MagicMock(),
        action_ladder=MagicMock(),
        rollback_registry=MagicMock(),
        library=MagicMock(),
    )


# ── _adapt_tool_result ────────────────────────────────────────────────────────


def test_adapt_tool_result_error():
    assert _adapt_tool_result("Error: pod not found") == {
        "status": "failed",
        "message": "Error: pod not found",
    }


def test_adapt_tool_result_success():
    res = "deployment.apps/web restarted"
    assert _adapt_tool_result(res) == {"status": "executed", "message": res}


# ── _compare ──────────────────────────────────────────────────────────────────


def test_compare_operators():
    cmp = RunbookExecutor._compare
    assert cmp(0.95, "gt", 0.9)
    assert not cmp(0.85, "gt", 0.9)
    assert cmp(5, "gte", 5)
    assert cmp(1, "lt", 2)
    assert not cmp(None, "lt", 2)  # missing value never passes
    assert not cmp(1, "bogus_op", 2)


# ── _materialize_llm_pending ──────────────────────────────────────────────────


def test_materialize_llm_pending_known_tool(executor: RunbookExecutor):
    pending = PendingApproval(
        approval_id="a1",
        runbook_id="llm_dynamic",
        action_type="scale_resource",
        target="default/web",
        incident_id="inc-1",
        healing_level=2,
        confidence=0.7,
        enqueued_at="now",
        context={
            "proposed_tool": "scale_resource",
            "tool_args": {
                "namespace": "prod",
                "resource_name": "web",
                "replicas": 5,
            },
            "cluster_summary": {"namespace": "prod", "primary_resource": "web"},
        },
    )
    runbook, action, event = executor._materialize_llm_pending(pending)
    # Level + blast radius come from the LLM_TOOL_LEVEL mapping.
    assert runbook.healing_level == 2
    assert runbook.blast_radius == "single_deployment"
    assert action.type == "scale_resource"
    assert event.namespace == "prod"
    assert event.resource_name == "web"


def test_materialize_llm_pending_unknown_tool_raises(executor: RunbookExecutor):
    pending = PendingApproval(
        approval_id="a2",
        runbook_id="llm_dynamic",
        action_type="rm_rf_root",
        target="default/x",
        incident_id="inc-2",
        healing_level=3,
        confidence=0.9,
        enqueued_at="now",
        context={},
    )
    with pytest.raises(ValueError, match="no governance level mapping"):
        executor._materialize_llm_pending(pending)
