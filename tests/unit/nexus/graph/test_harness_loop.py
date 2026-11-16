"""
NEXUS Harness Loop Integration & Unit Tests
===========================================
Tests the governed Evaluator-Optimizer diagnostic reflection loop,
the verification-driven adaptive recovery ladder, and pre-flight self-healing.
"""

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from nexus.graph.nodes.plan import plan_remediation_node
from nexus.graph.platform import HealthCheckResult, get_platform_registry
from nexus.graph.workflow import IncidentWorkflow

# ── 1. Verification-Driven Adaptive Recovery Ladder Tests ─────────────────────


def test_adaptive_recovery_ladder_escalation_on_k8s_restart_failure():
    """
    Tests that on Attempt 1 (retry), if a prior rollout restart failed post-checks
    for an application crash/bad_deploy, the planner escalates to L3 rollback with approval.
    """
    state = {
        "incident_id": "inc-adaptive-01",
        "platform": "kubernetes",
        "target": {"name": "order-service", "namespace": "default"},
        "diagnosis": {
            "failure_class": "bad_deploy",
            "root_cause": "CrashLoopBackOff after release v3.0",
            "confidence": 0.88,
        },
        "retry_count": 1,
        "verification": {
            "healthy": False,
            "failure_reason": "Pod order-service CrashLoopBackOff: exit code 1",
        },
        "execution_records": [
            {
                "step_index": 0,
                "tool_name": "k8s_restart_deployment",
                "success": True,
            }
        ],
    }

    result = plan_remediation_node(state)
    plan = result["plan"]

    # Must escalate to rollback deployment
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_rollback_deployment"
    assert plan["requires_approval"] is True
    assert "Adaptive Recovery" in plan["approval_reason"]
    assert "failed post-check" in plan["approval_reason"]

    # History tracking
    assert len(result["remediation_history"]) == 1
    history = result["remediation_history"][0]
    assert history["is_adaptive_escalation"] is True
    assert history["attempt"] == 1
    assert "k8s_restart_deployment" in history["prior_tools"]


def test_adaptive_recovery_ladder_escalation_on_aws_lambda():
    """
    Tests that on AWS, if memory increase failed and function is still timing out,
    the adaptive ladder escalates to timeout increase.
    """
    state = {
        "incident_id": "inc-adaptive-02",
        "platform": "aws",
        "target": {"name": "payment-processor", "namespace": "us-east-1"},
        "diagnosis": {
            "failure_class": "resource_exhaustion",
            "root_cause": "Lambda duration spike",
            "confidence": 0.85,
        },
        "telemetry": {"live_config": {"Timeout": 30, "MemorySize": 512}},
        "retry_count": 1,
        "verification": {
            "healthy": False,
            "failure_reason": "Task timed out after 30.00 seconds",
        },
        "execution_records": [
            {
                "step_index": 0,
                "tool_name": "aws_update_lambda_memory",
                "success": True,
            }
        ],
    }

    result = plan_remediation_node(state)
    plan = result["plan"]

    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "aws_update_lambda_timeout"
    assert step["parameters"]["timeout_seconds"] == 60


# ── 3. Pre-Flight Live-State Self-Healing Short-Circuit Tests ──────────────────


# ── 4. End-to-End Workflow with Adaptive Ladder & Human Approval ──────────────


class MockHarnessK8sModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "mock-harness-k8s"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "RCAReport",
                    "args": {
                        "root_cause_summary": "Application crash loop detected",
                        "affected_resource_urn": "k8s://production/apps/v1/Deployment/checkout-web",
                        "provider": "kubernetes",
                        "confidence": 0.95,
                        "hypotheses_evaluated": ["crash", "bad_deploy"],
                        "evidence_chain": [
                            {
                                "tool_name": "k8s_get_logs",
                                "tool_arguments": {},
                                "key_finding": "crashloopbackoff",
                            }
                        ],
                        "pre_incident_resource_state": {"replicas": 1},
                        "requires_remediation": True,
                        "suggested_action": "k8s_restart_deployment",
                    },
                    "id": "rca_wf_harness_1",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, *args: Any, **kwargs: Any) -> Any:
        return self


@pytest.mark.asyncio
async def test_e2e_workflow_adaptive_ladder_pause_and_resume():
    """
    Tests an end-to-end incident run where Attempt 0 fails, Attempt 1 escalates to L3
    rollback, pauses for human approval, and upon operator approval executes and resolves.
    """
    workflow = IncidentWorkflow()
    adapter = get_platform_registry().get("kubernetes")

    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-web",
            "namespace": "production",
            "severity": "critical",
            "signal_type": "pod_crashloop",
        },
    ]

    # Attempt 0 (restart plan) fails health checks for the entire poll window;
    # Attempt 1 (rollback plan) passes immediately.
    async def mock_health_check(target, plan):
        steps = (plan or {}).get("steps") or []
        tool = steps[0].get("tool_name", "") if steps else ""
        if tool == "k8s_restart_deployment":
            return HealthCheckResult(
                healthy=False,
                slo_restored=False,
                failure_reason="CrashLoopBackOff exit code 137",
            )
        return HealthCheckResult(
            healthy=True, slo_restored=True, details="Deployment healthy and ready"
        )

    rolled_back = False

    def mock_rollback(*args: Any, **kwargs: Any) -> str:
        nonlocal rolled_back
        rolled_back = True
        return "rolled_back"

    def mock_describe(*args: Any, **kwargs: Any) -> str:
        if rolled_back:
            return "Replicas: 1\nState: Running\nReady: 1/1"
        return "Replicas: 1\nState: CrashLoopBackOff"

    with (
        patch("nexus.graph.nodes.diagnose.get_llm", return_value=MockHarnessK8sModel()),
        patch(
            "nexus.tools.k8s.observe_tools.describe_resource", side_effect=mock_describe
        ),
        patch(
            "nexus.tools.k8s.mutate_tools.restart_deployment", return_value="restarted"
        ),
        patch(
            "nexus.tools.k8s.mutate_tools.rollback_deployment",
            side_effect=mock_rollback,
        ),
        patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05),
        patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.01),
        patch.object(adapter, "verify_health", side_effect=mock_health_check),
    ):
        # Initial run: Attempt 0 plans restart -> pauses at approval (all actions require approval)
        initial_run = await workflow.run_incident(
            {
                "incident_id": "inc-adaptive-e2e",
                "platform": "kubernetes",
                "events": events,
                "max_retries": 2,
            },
            thread_id="thread-adaptive-e2e",
        )

        assert initial_run["fsm_state"] == "approval_pending"
        assert initial_run["resolved"] is False
        assert initial_run["plan"]["steps"][0]["tool_name"] == "k8s_restart_deployment"

        # 1st resume with operator approval: Attempt 0 executes restart -> fails verify -> Attempt 1 escalates to rollback -> pauses for approval
        escalated_run = await workflow.resume_incident(
            thread_id="thread-adaptive-e2e",
            approval_decision="approved",
        )

        assert escalated_run["fsm_state"] == "approval_pending"
        assert escalated_run["resolved"] is False
        assert (
            escalated_run["plan"]["steps"][0]["tool_name"] == "k8s_rollback_deployment"
        )

        # 2nd resume with operator approval: Attempt 1 executes rollback -> passes verify -> resolved
        resumed_run = await workflow.resume_incident(
            thread_id="thread-adaptive-e2e",
            approval_decision="approved",
        )

        assert resumed_run["fsm_state"] == "resolved"
        assert resumed_run["resolved"] is True
        assert resumed_run["escalated"] is False
        # Both tools were executed across the two ladder attempts
        executed_tools = [rec["tool_name"] for rec in resumed_run["execution_records"]]
        assert "k8s_restart_deployment" in executed_tools
        assert "k8s_rollback_deployment" in executed_tools


# ── 4. Dynamic Context-Driven Scenario Unit Tests ─────────────────────────────


def test_dynamic_k8s_scaling_plan_from_traffic_spike():
    """Verify that a traffic surge dynamically computes scale replicas from current replicas."""
    state = {
        "incident_id": "inc-dynamic-scale-01",
        "platform": "kubernetes",
        "target": {"name": "payments-svc", "namespace": "prod"},
        "diagnosis": {
            "failure_class": "cascading_failure",
            "root_cause": "Traffic spike in payments-svc exceeding workload capacity",
            "confidence": 0.85,
        },
        "telemetry": {
            "live_config": {"replicas": 2},
            "metrics": {"current_replicas": 2, "traffic_ratio": 2.5},
        },
        "events": [
            {
                "signal_type": "traffic_spike",
                "resource_name": "payments-svc",
                "severity": "warning",
            }
        ],
    }

    result = plan_remediation_node(state)
    plan = result["plan"]
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_scale_resource"
    assert step["parameters"]["replicas"] == 5  # 2 * 2.5 = 5
    assert step["rollback_tool"] == "k8s_scale_resource"
    assert step["rollback_parameters"]["replicas"] == 2


def test_dynamic_k8s_patch_limits_from_oom_and_spec():
    """Verify that an OOMKilled incident dynamically scales container memory limit."""
    state = {
        "incident_id": "inc-dynamic-oom-01",
        "platform": "kubernetes",
        "target": {"name": "data-indexer", "namespace": "prod"},
        "diagnosis": {
            "failure_class": "resource_exhaustion",
            "root_cause": "Resource memory limit exceeded for data-indexer (exit code 137).",
            "confidence": 0.90,
        },
        "telemetry": {
            "health_status": "oomkilled",
            "live_config": {"memory_limit": "256Mi", "exit_code": 137},
            "recent_logs": ["Terminated with exit code 137", "oomkilled"],
        },
    }

    result = plan_remediation_node(state)
    plan = result["plan"]
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_patch_resource_limits"
    assert step["parameters"]["memory_limit"] == "512Mi"
    assert step["rollback_tool"] == "k8s_patch_resource_limits"
    assert step["rollback_parameters"]["memory_limit"] == "256Mi"


def test_dynamic_k8s_remove_command_override_on_command_failure():
    """Verify that a broken entrypoint/command override dynamically plans command removal."""
    state = {
        "incident_id": "inc-dynamic-cmd-01",
        "platform": "kubernetes",
        "target": {"name": "auth-service", "namespace": "default"},
        "diagnosis": {
            "failure_class": "bad_deploy",
            "root_cause": "Broken startup script or invalid command override in auth-service.",
            "confidence": 0.88,
        },
        "telemetry": {
            "live_config": {"has_command_override": True, "exit_code": 127},
            "recent_logs": ["/bin/sh: invalid_entrypoint: command not found"],
        },
    }

    result = plan_remediation_node(state)
    plan = result["plan"]
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_remove_container_command"
    assert step["rollback_tool"] == "k8s_restart_deployment"
