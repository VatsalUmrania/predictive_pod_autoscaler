"""
Unit Tests for Phase 5: Zero-Trust Governance & Locking
=======================================================
Verifies:
  1. OPA Rego rules via PolicyEngine:
     - Hard deny if action is destructive (is_destructive == True)
     - Hard deny if action has account_wide blast radius
     - Mandatory human approval for production + service_wide blast radius
     - Mandatory human approval if missing rollback and allows_unbacked_rollback == False
     - Auto-approval when rollback is provided or unbacked rollback is permitted
  2. PostgreSQL advisory locking in govern_node (zlib.crc32 keyed locking)
  3. Batch metadata enrichment from ToolRegistry in govern_node
"""

from __future__ import annotations

import zlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# 1. PolicyEngine & Rego Rule Tests
# ─────────────────────────────────────────────────────────────────────────────
import nexus.governance.policy_engine as pe_module
from nexus.governance.policy_abac import (
    build_opa_input_for_action,
    evaluate_local_abac,
)
from nexus.governance.policy_engine import PolicyEngine
from nexus.graph.nodes.govern import govern_node
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.registry import default_registry


def _patch_governance_opa(
    monkeypatch, allow: bool, require_approval: bool, deny_reasons: list[str]
):
    """Patches PolicyEngine HTTP client to simulate OPA /v1/data/nexus/governance endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("governance"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "allow": allow,
                        "require_human_approval": require_approval,
                        "deny_reasons": deny_reasons,
                    }
                },
            )
        # Legacy fallback endpoints
        if path.endswith("allow_action"):
            return httpx.Response(200, json={"result": allow})
        if path.endswith("deny_reasons"):
            return httpx.Response(200, json={"result": deny_reasons})
        if path.endswith("requires_human_approval"):
            return httpx.Response(200, json={"result": require_approval})
        return httpx.Response(404)

    original_client = pe_module.httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(pe_module.httpx, "AsyncClient", make_client)


@pytest.mark.asyncio
async def test_policy_engine_hard_deny_destructive_action(monkeypatch):
    """Verify that an action with is_destructive == True is denied via /governance endpoint."""
    _patch_governance_opa(
        monkeypatch,
        allow=False,
        require_approval=False,
        deny_reasons=["action_is_destructive_blocked"],
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        tool_name="aws_rds_delete_db_instance",
        is_destructive=True,
        blast_radius="database",
        allows_unbacked_rollback=False,
        has_rollback=False,
    )

    assert decision.allowed is False
    assert "action_is_destructive_blocked" in decision.deny_reasons
    assert decision.source == "opa"


@pytest.mark.asyncio
async def test_policy_engine_hard_deny_account_wide_blast_radius(monkeypatch):
    """Verify that an action with blast_radius == 'account_wide' is denied via /governance endpoint."""
    _patch_governance_opa(
        monkeypatch,
        allow=False,
        require_approval=False,
        deny_reasons=["blast_radius_account_wide_blocked"],
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        tool_name="aws_delete_vpc",
        is_destructive=False,
        blast_radius="account_wide",
        allows_unbacked_rollback=False,
        has_rollback=False,
    )

    assert decision.allowed is False
    assert "blast_radius_account_wide_blocked" in decision.deny_reasons


@pytest.mark.asyncio
async def test_policy_engine_requires_approval_production_service_wide(monkeypatch):
    """Verify that production + service_wide requires human approval."""
    _patch_governance_opa(
        monkeypatch,
        allow=True,
        require_approval=True,
        deny_reasons=[],
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        tool_name="aws_dynamodb_update_table",
        is_destructive=False,
        blast_radius="service_wide",
        environment="production",
        allows_unbacked_rollback=False,
        has_rollback=True,
    )

    assert decision.allowed is True
    assert decision.requires_approval is True


@pytest.mark.asyncio
async def test_policy_engine_missing_rollback_requires_approval(monkeypatch):
    """Verify that an action without rollback requires approval when allows_unbacked_rollback is False."""
    _patch_governance_opa(
        monkeypatch,
        allow=True,
        require_approval=True,
        deny_reasons=[],
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        tool_name="k8s_patch_resource_limits",
        is_destructive=False,
        blast_radius="single_deployment",
        allows_unbacked_rollback=False,
        has_rollback=False,  # missing rollback step
    )

    assert decision.allowed is True
    assert decision.requires_approval is True


@pytest.mark.asyncio
async def test_policy_engine_allows_unbacked_rollback_permitted(monkeypatch):
    """Verify that an action with allows_unbacked_rollback == True is permitted without mandatory approval."""
    _patch_governance_opa(
        monkeypatch,
        allow=True,
        require_approval=False,
        deny_reasons=[],
    )

    engine = PolicyEngine()
    decision = await engine.evaluate(
        tool_name="k8s_restart_deployment",
        is_destructive=False,
        blast_radius="single_deployment",
        allows_unbacked_rollback=True,
        has_rollback=False,
    )

    assert decision.allowed is True
    assert decision.requires_approval is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. govern_node Metadata Enrichment & Zero-Trust Defense-in-Depth
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_govern_node_enforces_zero_trust_metadata_for_destructive_tool():
    """Verify govern_node intercepts destructive tools via ToolRegistry metadata and blocks execution."""
    state = {
        "incident_id": "inc-gov-dest-01",
        "platform": "aws",
        "namespace": "us-east-1",
        "resource_kind": "RDS",
        "service_name": "orders-db",
        "confidence": 0.92,
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "aws_rds_delete_db_instance",
                    "tool_args": {"db_instance_identifier": "orders-db"},
                    "blast_radius": "database",
                    "reversible": False,
                }
            ],
        },
    }

    result = await govern_node(state)
    decision = result["policy_decision"]

    assert decision["allowed"] is False
    assert result["status"] == "POLICY_DENIED"
    assert result["approval_status"] == "POLICY_DENIED"
    assert result["approved"] is None
    assert "destructive" in decision["reason"].lower()


@pytest.mark.asyncio
async def test_govern_node_allows_k8s_remove_container_command_with_approval():
    """k8s_remove_container_command declares is_destructive=False and must require human approval, not DENY."""
    from nexus.governance.policy_abac import resolve_is_destructive
    from nexus.graph.nodes.escalate import derive_escalation_reason

    assert resolve_is_destructive("k8s_remove_container_command") is False
    assert resolve_is_destructive("aws_rds_delete_db_instance") is True
    assert resolve_is_destructive("custom_drop_table") is True

    state = {
        "incident_id": "CL-A4F56BE9",
        "platform": "k8s",
        "namespace": "default",
        "resource_kind": "Deployment",
        "service_name": "shop-demo",
        "confidence": 1.0,
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_remove_container_command",
                    "tool_args": {
                        "namespace": "default",
                        "deployment_name": "shop-demo",
                        "container_name": "shop-demo",
                    },
                    "blast_radius": "service_wide",
                    "reversible": True,
                }
            ]
        },
    }

    result = await govern_node(state)
    decision = result["policy_decision"]
    assert decision["allowed"] is True
    assert result["requires_approval"] is True
    assert result["approval_status"] == "PENDING"
    assert result["status"] == "AWAITING_APPROVAL"

    # Also verify OPA denial is never misreported as human approval rejection
    denied_state = {
        "status": "POLICY_DENIED",
        "approval_status": "POLICY_DENIED",
        "approved": None,
        "policy_decision": {"allowed": False, "reason": "Tool is destructive"},
    }
    esc_reason = derive_escalation_reason(denied_state)
    assert esc_reason.startswith("OPA Policy Denied:")
    assert "Human approval rejected" not in esc_reason


@pytest.mark.asyncio
async def test_govern_node_enforces_zero_trust_metadata_for_account_wide_tool():
    """Verify govern_node intercepts account_wide blast radius and blocks execution."""

    class MockAccountWideTool(NexusTool):
        name = "mock_account_wipe"
        description = "Dangerous account-level operation"
        domain = ToolDomain.AWS
        risk_level = ToolRiskLevel.L3_DESTRUCTIVE
        blast_radius = "account_wide"
        is_destructive = False
        allows_unbacked_rollback = False

        async def execute(self, **kwargs: Any) -> NexusToolResult:
            return NexusToolResult(success=True)

    default_registry.register(MockAccountWideTool())

    state = {
        "incident_id": "inc-gov-account-01",
        "platform": "aws",
        "namespace": "us-east-1",
        "resource_kind": "Account",
        "service_name": "account-root",
        "confidence": 0.9,
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "mock_account_wipe",
                    "tool_args": {},
                    "blast_radius": "account_wide",
                    "reversible": True,
                }
            ],
        },
    }

    try:
        result = await govern_node(state)
        decision = result["policy_decision"]

        assert decision["allowed"] is False
        assert result["status"] == "POLICY_DENIED"
        assert "account_wide" in decision["reason"]
    finally:
        default_registry.unregister("mock_account_wipe")


# ─────────────────────────────────────────────────────────────────────────────
# 3. PostgreSQL Advisory Locking Helper Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_govern_node_advisory_lock_contention_blocks():
    """Verify acquire_advisory_lock reports False when PostgreSQL advisory lock is busy."""
    from nexus.graph.nodes.govern_helpers import (
        acquire_advisory_lock,
        release_advisory_lock,
    )

    target_service = "payments-api"
    lock_key = zlib.crc32(target_service.encode())

    mock_conn = MagicMock()
    mock_conn.fetchval = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock()

    class MockAsyncContext:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_db_client = MagicMock()
    mock_db_client.acquire.return_value = MockAsyncContext()

    with patch(
        "nexus.graph.nodes.govern_helpers.get_database_client",
        AsyncMock(return_value=mock_db_client),
    ):
        conn, conn_ctx, key, acquired = await acquire_advisory_lock(target_service)
        await release_advisory_lock(conn, conn_ctx, key, acquired)

    assert acquired is False
    assert key == lock_key
    mock_conn.fetchval.assert_awaited_once_with(
        "SELECT pg_try_advisory_lock($1)", lock_key
    )
    mock_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_govern_node_advisory_lock_released_on_exit():
    """Verify release_advisory_lock unlocks an acquired PostgreSQL advisory lock."""
    from nexus.graph.nodes.govern_helpers import (
        acquire_advisory_lock,
        release_advisory_lock,
    )

    target_service = "checkout-api"
    lock_key = zlib.crc32(target_service.encode())

    mock_conn = MagicMock()
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.execute = AsyncMock()

    class MockAsyncContext:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_db_client = MagicMock()
    mock_db_client.acquire.return_value = MockAsyncContext()

    with patch(
        "nexus.graph.nodes.govern_helpers.get_database_client",
        AsyncMock(return_value=mock_db_client),
    ):
        conn, conn_ctx, key, acquired = await acquire_advisory_lock(target_service)
        await release_advisory_lock(conn, conn_ctx, key, acquired)

    assert acquired is True
    mock_conn.fetchval.assert_awaited_once_with(
        "SELECT pg_try_advisory_lock($1)", lock_key
    )
    mock_conn.execute.assert_awaited_once_with(
        "SELECT pg_advisory_unlock($1)", lock_key
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Plane 4: Strict OPA Precedence & AST Path Inspection Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_opa_ast_scale_to_zero_denied():
    """Verify modifying /spec/replicas to 0 is strictly denied by AST inspection."""
    action = {
        "tool_name": "k8s_scale_resource",
        "mutated_paths": ["/spec/replicas"],
        "patch_payload": {"spec": {"replicas": 0}},
        "blast_radius": "pod",
        "has_rollback": True,
        "rollback_tool_name": "k8s_scale_resource",
    }
    inp = build_opa_input_for_action(action)
    decision, reasons = evaluate_local_abac(inp)

    assert decision == "DENY"
    assert any("Scale to zero replicas is prohibited" in r for r in reasons)


def test_opa_ast_immutable_labels_denied():
    """Verify modifying system/immutable labels is strictly denied by AST inspection."""
    action = {
        "tool_name": "k8s_patch_labels",
        "mutated_paths": ["/metadata/labels/app.kubernetes.io/name"],
        "patch_payload": {"metadata": {"labels": {"app.kubernetes.io/name": "hacked"}}},
        "blast_radius": "pod",
        "has_rollback": True,
        "rollback_tool_name": "k8s_patch_labels",
    }
    inp = build_opa_input_for_action(action)
    decision, reasons = evaluate_local_abac(inp)

    assert decision == "DENY"
    assert any("Mutating system/immutable labels is prohibited" in r for r in reasons)


def test_opa_strict_precedence_deny_overrides_allow_auto():
    """Verify strict precedence: DENY takes precedence over ALLOW_AUTO."""
    action = {
        "tool_name": "aws_rds_delete_cluster",
        "is_destructive": True,
        "blast_radius": "pod",
        "allow_auto": True,
        "has_rollback": True,
        "rollback_tool_name": "noop",
    }
    inp = build_opa_input_for_action(action)
    inp["allow_auto"] = True
    decision, reasons = evaluate_local_abac(inp)

    assert decision == "DENY"
    assert len(reasons) > 0


def test_opa_auto_approval_tier():
    """Verify low-risk safe actions with high confidence auto-approve when permitted."""
    action = {
        "tool_name": "k8s_restart_pod",
        "blast_radius": "pod",
        "is_destructive": False,
        "has_rollback": True,
        "rollback_tool_name": "noop",
        "allow_auto": True,
    }
    inp = build_opa_input_for_action(action, rca_report={"confidence": 0.99})
    inp["allow_auto"] = True
    decision, reasons = evaluate_local_abac(inp)

    assert decision == "ALLOW_AUTO"
    assert len(reasons) == 0
