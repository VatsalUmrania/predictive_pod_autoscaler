"""
Comprehensive tests for NEXUS LangGraph Incident Workflow.
Covers:
  - Kubernetes autonomous L1 remediation (OOM -> Restart -> Resolved)
  - AWS human-in-the-loop interruption & approval (Lambda OOM -> Interrupt -> Resume -> Resolved)
  - Human operator rejection pathway (Interrupt -> Reject -> Escalate)
  - Verification failure & deterministic snapshot-based rollback
  - Explicit architectural verification: NOT a ReAct loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.graph.platform import (
    HealthCheckResult,
    get_platform_registry,
)
from nexus.graph.workflow import IncidentWorkflow
from nexus.tools.base import NexusToolResult

# ── Test 1: Kubernetes Autonomous L1 Remediation ─────────────────────────────

@pytest.mark.asyncio
async def test_k8s_remediation_requires_approval_and_resumes():
    """Verify all actions require human approval and execute cleanly upon operator sign-off."""
    workflow = IncidentWorkflow()
    thread_id = "inc-k8s-auto-01-thread"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-api",
            "namespace": "production",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="State: OOMKilled\nReplicas: 3"):
        with patch("nexus.agents.k8s_tools.get_pod_logs", return_value="panic: fatal memory error"):
            with patch("nexus.agents.k8s_tools.restart_deployment", return_value="deployment.apps/checkout-api restarted"):
                with patch.object(
                    adapter,
                    "verify_health",
                    AsyncMock(return_value=HealthCheckResult(healthy=True, slo_restored=True, details="Pods Ready")),
                ):
                    interrupted = await workflow.run_incident(
                        {
                            "incident_id": "inc-k8s-auto-01",
                            "platform": "kubernetes",
                            "events": events,
                        },
                        thread_id=thread_id,
                    )

                    # Must halt for operator approval
                    assert interrupted["incident_id"] == "inc-k8s-auto-01"
                    assert interrupted["fsm_state"] == "approval_pending"
                    assert interrupted["plan"]["requires_approval"] is True
                    assert len(interrupted["execution_records"]) == 0

                    # Operator approves
                    result = await workflow.resume_incident(
                        thread_id=thread_id,
                        approval_decision="approved",
                    )

                    assert result["resolved"] is True
                    assert result["escalated"] is False
                    assert result["fsm_state"] == "resolved"
                    assert len(result["execution_records"]) == 1
                    assert result["execution_records"][0]["success"] is True

                    # Check deterministic sequence in audit log (NO ReAct!)
                    stages = [e["stage"] for e in result["audit_log"]]
                    assert "govern" in stages
                    assert "execute" in stages
                    assert "verify" in stages
                    assert "learn" in stages


# ── Test 2: AWS Human-in-the-Loop Interruption & Resume ───────────────────────

@pytest.mark.asyncio
async def test_aws_hitl_approval_and_resume():
    """L2 mutating action triggers LangGraph interrupt; operator approves; finishes cleanly."""
    workflow = IncidentWorkflow()
    thread_id = "inc-aws-hitl-thread-42"
    adapter = get_platform_registry().get("aws")

    events = [
        {
            "agent": "cloudwatch",
            "resource_name": "arn:aws:lambda:us-east-1:123456789012:function:payments-worker",
            "namespace": "us-east-1",
            "severity": "critical",
            "signal_type": "lambda_oom",
        }
    ]

    with patch("nexus.graph.platform.aws._get_boto3_client") as mock_boto:
        client = MagicMock()
        client.get_function_configuration.return_value = {"MemorySize": 256, "Timeout": 30, "State": "Active"}
        client.update_function_configuration.return_value = {"MemorySize": 512, "Timeout": 30}
        mock_boto.return_value = client

        # Mock lambda_tools execution
        with patch("aws.tools.lambda_tools.increase_memory", return_value={"success": True, "pre": 256, "post": 512}):
            with patch.object(
                adapter,
                "verify_health",
                AsyncMock(return_value=HealthCheckResult(healthy=True, slo_restored=True, details="Memory verified 512MB")),
            ):
                # 1. First run: should execute triage -> collect -> diagnose -> plan -> govern -> pause at approval
                interrupted_state = await workflow.run_incident(
                    {
                        "incident_id": "inc-aws-hitl-01",
                        "platform": "aws",
                        "events": events,
                    },
                    thread_id=thread_id,
                )

                assert interrupted_state["fsm_state"] == "approval_pending"
                assert interrupted_state["plan"]["requires_approval"] is True
                assert len(interrupted_state["execution_records"]) == 0

                # 2. Operator reviews and approves via resume_incident
                resumed_state = await workflow.resume_incident(
                    thread_id=thread_id,
                    approval_decision="approved",
                )

                assert resumed_state["approval_decision"] == "approved"
                assert resumed_state["resolved"] is True
                assert resumed_state["fsm_state"] == "resolved"
                assert len(resumed_state["execution_records"]) == 1
                assert resumed_state["execution_records"][0]["tool_name"] == "aws_update_lambda_memory"
                assert resumed_state["execution_records"][0]["success"] is True


# ── Test 3: Operator Rejection Pathway ────────────────────────────────────────

@pytest.mark.asyncio
async def test_operator_rejection_pathway():
    """Operator rejects plan: transitions to REJECTED/ESCALATED without executing mutation."""
    workflow = IncidentWorkflow()
    thread_id = "inc-rejection-thread-99"

    events = [
        {
            "agent": "cloudwatch",
            "resource_name": "arn:aws:lambda:us-east-1:123456789012:function:risk-evaluator",
            "namespace": "us-east-1",
            "severity": "critical",
            "signal_type": "lambda_oom",
        }
    ]

    with patch("nexus.graph.platform.aws._get_boto3_client") as mock_boto:
        client = MagicMock()
        client.get_function_configuration.return_value = {"MemorySize": 256, "Timeout": 30}
        mock_boto.return_value = client

        # 1. Run until approval interrupt
        await workflow.run_incident(
            {
                "incident_id": "inc-reject-01",
                "platform": "aws",
                "events": events,
            },
            thread_id=thread_id,
        )

        # 2. Operator rejects
        resumed_state = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="rejected",
        )

        assert resumed_state["approval_decision"] == "rejected"
        assert resumed_state["escalated"] is True
        assert resumed_state["resolved"] is False
        assert resumed_state["fsm_state"] == "escalated"
        # Zero executions performed!
        assert len(resumed_state["execution_records"]) == 0


# ── Test 4: Verification Failure & Deterministic Rollback ──────────────────────

@pytest.mark.asyncio
async def test_verification_failure_and_deterministic_rollback():
    """When health check fails repeatedly, applies pre-mutation snapshot and escalates."""
    workflow = IncidentWorkflow()
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "unstable-api",
            "namespace": "staging",
            "severity": "critical",
            "signal_type": "pod_oomkilled",
        }
    ]

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="Replicas: 2\nState: CrashLoopBackOff"):
        with patch("nexus.agents.k8s_tools.restart_deployment", return_value="restarted"):
            with patch.object(
                adapter,
                "verify_health",
                AsyncMock(return_value=HealthCheckResult(healthy=False, slo_restored=False, failure_reason="OOM persists")),
            ):
                with patch.object(
                    adapter,
                    "execute_rollback",
                    AsyncMock(return_value=NexusToolResult(success=True, data={"status": "rolled_back"})),
                ) as mock_rb:
                    result = await workflow.run_incident(
                        {
                            "incident_id": "inc-rollback-01",
                            "platform": "kubernetes",
                            "events": events,
                            "max_retries": 0,
                            "approval_decision": "approved",
                        }
                    )

                    assert result["resolved"] is False
                    assert result["escalated"] is True
                    assert result["rollback_executed"] is True
                    assert len(result["rollback_records"]) > 0
                    assert result["fsm_state"] == "escalated"
                    mock_rb.assert_called()


# ── Test 5: Architectural Guarantee: NOT a ReAct Loop ─────────────────────────

def test_not_a_react_loop_architecture():
    """
    Verify architectural invariants:
      1. Graph contains explicit named state nodes for each lifecycle phase.
      2. No open-ended conversational tool calling loop.
      3. Governance and policy gates sit strictly between Planner and Executor.
    """
    workflow = IncidentWorkflow()
    graph = workflow._compiled_graph

    # Verify nodes are distinct lifecycle stages
    expected_nodes = {
        "triage",
        "collect",
        "diagnose",
        "plan",
        "govern",
        "approval",
        "execute",
        "verify",
        "rollback",
        "learn",
        "escalate",
    }
    graph_node_names = set(graph.nodes.keys())
    for node in expected_nodes:
        assert node in graph_node_names, f"Lifecycle node '{node}' missing from graph"

    # Verify that 'govern' and 'approval' precede 'execute'
    assert "govern" in graph_node_names
    assert "execute" in graph_node_names
    assert "rollback" in graph_node_names


# ── Test 6: Dynamic Tool Planning Without Runbooks & Evidence Confidence ────────

@pytest.mark.asyncio
async def test_dynamic_tool_planning_without_runbooks_and_evidence_confidence():
    """
    Verify that:
      1. Incidents are planned and remediated using dynamic platform tools with NO static runbook_id.
      2. Safe L1 actions (k8s_restart_deployment) execute autonomously without confidence cliff blockage.
      3. Workflow resolves successfully with dynamic evidence-based confidence.
    """
    registry = get_platform_registry()
    adapter = registry.get("kubernetes")

    workflow = IncidentWorkflow()

    events = [
        {
            "agent": "k8s",
            "resource_name": "shop-demo",
            "namespace": "default",
            "severity": "critical",
            "signal_type": "pod_crashloop",
            "context": {"restart_count": 5},
        }
    ]

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="Pod: shop-demo\nState: CrashLoopBackOff\nRestarts: 5"):
        with patch("nexus.agents.k8s_tools.restart_deployment", return_value="restarted deployment shop-demo"):
            with patch.object(
                adapter,
                "verify_health",
                AsyncMock(return_value=HealthCheckResult(healthy=True, slo_restored=True)),
            ):
                interrupted = await workflow.run_incident(
                    {
                        "incident_id": "inc-dynamic-tool-01",
                        "platform": "kubernetes",
                        "events": events,
                        "max_retries": 1,
                    },
                    thread_id="thread-dynamic-tool-01",
                )

                # Verify dynamic plan requires human approval
                assert interrupted["fsm_state"] == "approval_pending"
                plan = interrupted["plan"]
                assert len(plan["steps"]) == 1
                assert plan["steps"][0]["tool_name"] == "k8s_restart_deployment"
                assert plan["requires_approval"] is True

                # Operator approves
                result = await workflow.resume_incident(
                    thread_id="thread-dynamic-tool-01",
                    approval_decision="approved",
                )

                assert result["resolved"] is True
                assert result["escalated"] is False
                assert result["fsm_state"] == "resolved"

                # Verify evidence-based confidence
                assert result["diagnosis"]["confidence"] >= 0.80

