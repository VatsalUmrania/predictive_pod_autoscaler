"""
Tier-1 incident-flow wiring tests for the zero-hardcoding agentic pipeline:
  1. ReAct verify_node multi-turn live observation & structured VerificationResult
  2. Attribute-based OPA governance (deny, require approval, auto-approve)
  3. Schema-validated execution & LIFO rollback via ToolRegistry
  4. Workflow completion records outcomes to KnowledgeBase and enforces report guard
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from nexus.engine.nodes.execute import execute_node
from nexus.engine.nodes.govern import govern_node
from nexus.engine.nodes.rollback import rollback_node
from nexus.engine.nodes.verify import verify_node
from nexus.engine.reasoning.rca.result import VerificationResult
from nexus.engine.workflow.workflow import IncidentWorkflow


@pytest.fixture
def status_context():
    from nexus.services.api.status_api import context

    saved = (
        context.action_ladder,
        context.audit_trail,
        context.knowledge_base,
    )
    yield context
    (
        context.action_ladder,
        context.audit_trail,
        context.knowledge_base,
    ) = saved


def _state(**overrides):
    base = {
        "incident_id": "inc-wire-01",
        "platform": "k8s",
        "namespace": "shop-ns",
        "resource_kind": "Deployment",
        "service_name": "svc-a",
        "target_resource": "shop-ns/Deployment/svc-a",
        "confidence": 0.9,
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_restart_deployment",
                    "tool_args": {"namespace": "shop-ns", "deployment_name": "svc-a"},
                    "blast_radius": "pod",
                    "reversible": True,
                }
            ]
        },
        "execution_results": [
            {
                "step": 1,
                "tool_name": "k8s_restart_deployment",
                "tool_args": {"namespace": "shop-ns", "deployment_name": "svc-a"},
                "status": "SUCCESS",
            }
        ],
        "remediation_attempts": 1,
        "max_remediation_attempts": 2,
    }
    base.update(overrides)
    return base


# ── 1. ReAct Verification Node ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_react_observes_until_healthy():
    """verify_node iterates ReAct tool calls and marks RESOLVED when healthy."""
    observe_tool = MagicMock()
    observe_tool.name = "inspect_deployment_status"
    observe_tool.ainvoke = AsyncMock(
        side_effect=[
            {"desired_replicas": 2, "ready_replicas": 1},
            {"desired_replicas": 2, "ready_replicas": 2},
        ]
    )
    mock_registry = MagicMock()
    mock_registry.get_tools.return_value = [observe_tool]

    bound_llm = MagicMock()
    bound_llm.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "v1",
                        "name": "inspect_deployment_status",
                        "args": {"deployment_name": "svc-a", "namespace": "shop-ns"},
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "v2",
                        "name": "inspect_deployment_status",
                        "args": {"deployment_name": "svc-a", "namespace": "shop-ns"},
                    }
                ],
            ),
            AIMessage(content="Healthy", tool_calls=[]),
        ]
    )
    struct_llm = MagicMock()
    struct_llm.ainvoke = AsyncMock(
        return_value=VerificationResult(
            verification_passed=True, observed_state_summary="Ready 2/2"
        )
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = bound_llm
    mock_llm.with_structured_output.return_value = struct_llm

    with (
        patch(
            "nexus.engine.nodes.verify.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.engine.nodes.verify.get_chat_model", return_value=mock_llm),
    ):
        result = await verify_node(_state())

    assert result["verification_passed"] is True
    assert result["status"] == "RESOLVED"
    assert observe_tool.ainvoke.await_count == 2


@pytest.mark.asyncio
async def test_verify_failure_returns_verification_feedback():
    """Unhealthy observation sets verification_passed=False and preserves failure feedback."""
    mock_registry = MagicMock()
    mock_registry.get_tools.return_value = []

    struct_llm = MagicMock()
    struct_llm.ainvoke = AsyncMock(
        return_value=VerificationResult(
            verification_passed=False,
            observed_state_summary="0/2 replicas ready",
            failure_feedback="OOM persists after restart",
        )
    )
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="Done", tool_calls=[]))
    mock_llm.with_structured_output.return_value = struct_llm

    with (
        patch(
            "nexus.engine.nodes.verify.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.engine.nodes.verify.get_chat_model", return_value=mock_llm),
    ):
        result = await verify_node(_state())

    assert result["verification_passed"] is False
    assert result["status"] == "VERIFICATION_FAILED"
    assert "OOM persists" in result["verification_feedback"]


# ── 2. Attribute-Based OPA Governance ────────────────────────────────────────


@pytest.mark.asyncio
async def test_govern_blocks_when_opa_denies():
    with patch(
        "nexus.engine.nodes.govern.evaluate_opa_policy",
        AsyncMock(
            return_value={
                "allowed": False,
                "requires_approval": False,
                "reason": "Circuit breaker tripped",
            }
        ),
    ):
        result = await govern_node(_state())

    assert result["policy_decision"]["allowed"] is False
    assert result["approval_status"] == "POLICY_DENIED"
    assert result["approved"] is None
    assert result["status"] == "POLICY_DENIED"


@pytest.mark.asyncio
async def test_govern_requires_approval_and_resets_flags():
    with patch(
        "nexus.engine.nodes.govern.evaluate_opa_policy",
        AsyncMock(
            return_value={
                "allowed": True,
                "requires_approval": True,
                "reason": "Human authorization required",
            }
        ),
    ):
        result = await govern_node(_state(approved=True, approved_by="old"))

    assert result["requires_approval"] is True
    assert result["approved"] is None
    assert result["approved_by"] is None
    assert result["approval_status"] == "PENDING"
    assert result["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_govern_auto_approves_when_opa_allows_without_approval():
    with patch(
        "nexus.engine.nodes.govern.evaluate_opa_policy",
        AsyncMock(
            return_value={
                "allowed": True,
                "requires_approval": False,
                "reason": "Auto-approved",
            }
        ),
    ):
        result = await govern_node(_state())

    assert result["policy_decision"]["allowed"] is True
    assert result["requires_approval"] is False
    assert result["approved"] is True
    assert result["approval_status"] == "AUTO_APPROVED"
    assert result["status"] == "APPROVED"


# ── 3. Direct Execution & LIFO Rollback ──────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_invokes_tool_and_sets_rollback_available():
    mock_tool = MagicMock()
    mock_tool.ainvoke = AsyncMock(return_value="Scaled svc-a to 4")
    mock_registry = MagicMock()
    mock_registry.get_tool.return_value = mock_tool

    state = _state(
        execution_results=[],
        remediation_attempts=0,
        remediation_plan={
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_scale_resource",
                    "tool_args": {
                        "namespace": "shop-ns",
                        "resource_name": "svc-a",
                        "replicas": 4,
                    },
                    "reversible": True,
                }
            ]
        },
    )

    with patch(
        "nexus.engine.nodes.execute.get_tool_registry", return_value=mock_registry
    ):
        result = await execute_node(state)

    assert result["execution_status"] == "SUCCESS"
    assert result["rollback_available"] is True
    assert result["remediation_attempts"] == 1
    assert result["execution_results"][0]["tool_name"] == "k8s_scale_resource"


@pytest.mark.asyncio
async def test_rollback_executes_declared_plan_step_inverse():
    rb_tool = MagicMock()
    rb_tool.ainvoke = AsyncMock(return_value="Reverted to 2 replicas")
    mock_registry = MagicMock()
    mock_registry.get_tool.return_value = rb_tool

    state = _state(
        remediation_plan={
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_scale_resource",
                    "tool_args": {
                        "namespace": "shop-ns",
                        "resource_name": "svc-a",
                        "replicas": 4,
                    },
                    "rollback_tool": "k8s_scale_resource",
                    "rollback_args": {
                        "namespace": "shop-ns",
                        "resource_name": "svc-a",
                        "replicas": 2,
                    },
                }
            ]
        },
        execution_results=[
            {
                "step": 1,
                "tool_name": "k8s_scale_resource",
                "tool_args": {
                    "namespace": "shop-ns",
                    "resource_name": "svc-a",
                    "replicas": 4,
                },
                "status": "SUCCESS",
            }
        ],
    )

    with patch(
        "nexus.engine.nodes.rollback.get_tool_registry", return_value=mock_registry
    ):
        res = await rollback_node(state)

    assert res["status"] == "ROLLED_BACK"
    assert len(res["rollback_results"]) == 1
    assert res["rollback_results"][0]["status"] == "SUCCESS"
    rb_tool.ainvoke.assert_awaited_once_with(
        {"namespace": "shop-ns", "resource_name": "svc-a", "replicas": 2}
    )


# ── 4. Learning-plane recording at workflow completion ────────────────────────


@pytest.mark.asyncio
async def test_completion_records_success_outcome_to_learning_plane(status_context):
    workflow = IncidentWorkflow()
    status_context.knowledge_base = MagicMock()
    status_context.knowledge_base.record_incident_outcome = AsyncMock()

    state = _state(
        status="RESOLVED",
        failure_class="oom_killed_memory_limit",
        verification_passed=True,
        verification_feedback="Ready 2/2",
    )

    with (
        patch(
            "nexus.services.observability.dashboard.record_incident_resolution",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ) as mock_report,
    ):
        await workflow._handle_workflow_completion(
            state, inc_id="inc-learn-01", thread_id="t1"
        )

    status_context.knowledge_base.record_incident_outcome.assert_awaited_once()
    io_kwargs = status_context.knowledge_base.record_incident_outcome.await_args.kwargs
    assert io_kwargs["resolved"] is True
    assert io_kwargs["runbook_id"] == "oom_killed_memory_limit"
    assert io_kwargs["action_type"] == "k8s_restart_deployment"
    mock_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_completion_records_failed_outcome_and_guards_empty_execution(
    status_context,
):
    workflow = IncidentWorkflow()
    status_context.knowledge_base = MagicMock()
    status_context.knowledge_base.record_incident_outcome = AsyncMock()

    state = _state(
        status="ESCALATED",
        failure_class="manual_intervention_needed",
        execution_results=[],
    )

    with (
        patch(
            "nexus.services.observability.dashboard.record_incident_resolution",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ) as mock_report,
    ):
        await workflow._handle_workflow_completion(
            state, inc_id="inc-learn-02", thread_id="t2"
        )

    status_context.knowledge_base.record_incident_outcome.assert_awaited_once()
    io_kwargs = status_context.knowledge_base.record_incident_outcome.await_args.kwargs
    assert io_kwargs["resolved"] is False
    # Zero executed steps -> Post-Incident Report strictly skipped
    mock_report.assert_not_called()
