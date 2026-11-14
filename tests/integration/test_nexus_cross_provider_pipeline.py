"""
Integration tests for NEXUS Cross-Provider Autonomous Incident Pipeline.
Tests end-to-end lifecycle across Kubernetes and AWS providers:
  Ingestion -> ReAct Diagnosis -> Autonomous Planning -> Zero-Trust Governance ->
  HITL Approval / Interrupt -> Governed Execution & Advisory Locking ->
  Verification Contract -> Learning & Audit Trail.
"""

from __future__ import annotations

import zlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bus.events.event import AnomalySignal
from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.execute import execute_node
from nexus.graph.platform import HealthCheckResult, TargetResource, get_platform_registry
from nexus.graph.workflow import IncidentWorkflow
from nexus.tools.base import NexusToolResult


@pytest.mark.asyncio
async def test_k8s_autonomous_cross_provider_pipeline_e2e():
    """Verify full end-to-end autonomous healing pipeline for Kubernetes."""
    workflow = IncidentWorkflow()
    thread_id = "thread-pipeline-k8s-01"
    adapter = get_platform_registry().get("kubernetes")

    signal = AnomalySignal(
        signal_id="sig-k8s-01",
        source="prometheus",
        provider="kubernetes",
        environment="production",
        target_service="payment-service",
        target_resource_id="payment-service",
        symptom_summary="CrashLoopBackOff detected with memory threshold breach",
        raw_metrics_or_alert={"container": "payment-api", "exit_code": 137},
    )

    with patch(
        "nexus.agents.k8s_tools.describe_resource",
        return_value="State: CrashLoopBackOff\nRestartCount: 5\nExitCode: 137 (OOMKilled)",
    ), patch(
        "nexus.agents.k8s_tools.get_pod_logs",
        return_value="fatal error: runtime: out of memory",
    ), patch(
        "nexus.agents.k8s_tools.restart_deployment",
        return_value="deployment.apps/payment-service restarted",
    ), patch.object(
        adapter,
        "verify_health",
        AsyncMock(
            return_value=HealthCheckResult(
                healthy=True, slo_restored=True, metrics={"restart_count": 0}
            )
        ),
    ):
        # 1. Ingest AnomalySignal & Run Graph
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-k8s-pipe-01",
                "platform": "kubernetes",
                "anomaly_signal": signal.model_dump(),
                "events": [
                    {
                        "agent": "k8s",
                        "resource_name": "payment-service",
                        "namespace": "production",
                        "severity": "critical",
                        "signal_type": "pod_crashloop",
                    }
                ],
            },
            thread_id=thread_id,
        )

        # 2. Verify paused at approval gate
        assert interrupted["incident_id"] == "inc-k8s-pipe-01"
        assert interrupted["fsm_state"] == "approval_pending"
        assert interrupted["plan"]["requires_approval"] is True
        assert len(interrupted["execution_records"]) == 0

        # Verify plan has verification condition
        plan = interrupted["plan"]
        assert len(plan["steps"]) >= 1

        # 3. Resume via HITL approval
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="approved",
        )

        # 4. Verify completed through verify & learn
        assert resumed["resolved"] is True
        assert resumed["fsm_state"] == "resolved"
        assert len(resumed["execution_records"]) >= 1
        assert resumed["execution_records"][0]["success"] is True

        # Verify stages traversed in order
        stages = [e["stage"] for e in resumed["audit_log"]]
        assert "govern" in stages
        assert "execute" in stages
        assert "verify" in stages
        assert "learn" in stages


@pytest.mark.asyncio
async def test_aws_autonomous_cross_provider_pipeline_e2e():
    """Verify full end-to-end autonomous healing pipeline for AWS."""
    workflow = IncidentWorkflow()
    thread_id = "thread-pipeline-aws-01"
    adapter = get_platform_registry().get("aws")

    signal = AnomalySignal(
        signal_id="sig-aws-01",
        source="cloudwatch",
        provider="aws",
        environment="production",
        target_service="order-processor-fn",
        target_resource_id="arn:aws:lambda:us-east-1:123456789012:function:order-processor-fn",
        symptom_summary="Lambda invocation timeouts exceeding SLO threshold",
        raw_metrics_or_alert={"Duration": 3000.0, "Errors": 15},
    )

    with patch.object(
        adapter,
        "execute_action",
        AsyncMock(
            return_value=NexusToolResult(
                success=True,
                data={"message": "Lambda function memory updated to 512MB"},
            )
        ),
    ), patch.object(
        adapter,
        "verify_health",
        AsyncMock(
            return_value=HealthCheckResult(
                healthy=True, slo_restored=True, metrics={"error_rate": 0.0}
            )
        ),
    ):
        # 1. Ingest AnomalySignal & Run Graph
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-aws-pipe-01",
                "platform": "aws",
                "anomaly_signal": signal.model_dump(),
                "events": [
                    {
                        "agent": "aws",
                        "resource_name": "order-processor-fn",
                        "namespace": "us-east-1",
                        "severity": "critical",
                        "signal_type": "lambda_timeout",
                    }
                ],
            },
            thread_id=thread_id,
        )

        # 2. Paused for human approval
        assert interrupted["fsm_state"] == "approval_pending"

        # 3. SRE Approves
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="approved",
        )

        # 4. Resolved successfully
        assert resumed["resolved"] is True
        assert resumed["fsm_state"] == "resolved"
        assert len(resumed["execution_records"]) >= 1
        assert resumed["execution_records"][0]["success"] is True


@pytest.mark.asyncio
async def test_cross_provider_advisory_locking_concurrency():
    """Verify advisory locking prevents concurrent mutations on the same service."""
    service_name = "shared-critical-service"
    lock_key = zlib.crc32(service_name.encode())

    mock_conn = MagicMock()
    mock_conn.fetchval = AsyncMock(return_value=False)  # Lock already held by another process

    class MockAsyncContext:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_db_client = MagicMock()
    mock_db_client.acquire.return_value = MockAsyncContext()

    state = {
        "incident_id": "inc-concurrent-01",
        "target": {"name": service_name, "namespace": "default", "platform": "kubernetes"},
        "platform": "kubernetes",
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_restart_deployment",
                    "parameters": {"deployment_name": service_name, "namespace": "default"},
                }
            ]
        },
    }

    with patch("nexus.graph.nodes.execute.get_database_client", AsyncMock(return_value=mock_db_client)):
        result = await execute_node(state)

        # Must be blocked by advisory lock contention
        assert result["fsm_state"] == IncidentState.FAILED.value
        assert "concurrent mutation" in result["error_message"]
        assert any("lock_contention" in e.get("action", "") for e in result["audit_log"])
        mock_conn.fetchval.assert_awaited_once_with("SELECT pg_try_advisory_lock($1)", lock_key)


@pytest.mark.asyncio
async def test_cross_provider_verification_failure_triggers_rollback():
    """Verify post-check verification failure triggers deterministic rollback and escalation."""
    workflow = IncidentWorkflow()
    thread_id = "thread-pipeline-rollback"
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "inventory-svc",
            "namespace": "production",
            "severity": "critical",
            "signal_type": "pod_crashloop",
        }
    ]

    with patch(
        "nexus.agents.k8s_tools.describe_resource",
        return_value="State: CrashLoopBackOff",
    ), patch(
        "nexus.agents.k8s_tools.get_pod_logs",
        return_value="fatal crash",
    ), patch(
        "nexus.agents.k8s_tools.restart_deployment",
        return_value="deployment restarted",
    ), patch(
        "nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05
    ), patch(
        "nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.01
    ), patch.object(
        adapter,
        "verify_health",
        AsyncMock(
            return_value=HealthCheckResult(
                healthy=False,
                slo_restored=False,
                reason="Pods Still Crashing after max retries",
            )
        ),
    ), patch.object(
        adapter,
        "execute_rollback",
        AsyncMock(
            return_value=NexusToolResult(
                success=True, data={"message": "Rolled back to previous revision"}
            )
        ),
    ) as mock_rb:
        interrupted = await workflow.run_incident(
            {
                "incident_id": "inc-rollback-01",
                "platform": "kubernetes",
                "events": events,
                "max_retries": 0,
            },
            thread_id=thread_id,
        )

        assert interrupted["fsm_state"] == "approval_pending"

        # Resume execution
        resumed = await workflow.resume_incident(
            thread_id=thread_id,
            approval_decision="approved",
        )

        # Verification fails -> triggers rollback -> escalates
        assert resumed["resolved"] is False
        assert resumed["escalated"] is True
        assert resumed["rollback_executed"] is True
        assert resumed["fsm_state"] == "escalated"
        mock_rb.assert_called()
        stages = [e["stage"] for e in resumed["audit_log"]]
        assert "execute" in stages
        assert "verify" in stages
        assert "rollback" in stages
        assert "escalate" in stages
