"""
NEXUS Harness Loop Integration & Unit Tests
===========================================
Tests the governed Evaluator-Optimizer diagnostic reflection loop,
the verification-driven adaptive recovery ladder, and pre-flight self-healing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.diagnose import diagnose_node
from nexus.graph.nodes.govern import govern_node
from nexus.graph.nodes.plan import plan_remediation_node
from nexus.graph.platform import HealthCheckResult, get_platform_registry
from nexus.graph.rca import RCAResult
from nexus.graph.workflow import IncidentWorkflow, route_after_govern

# ── 1. Diagnostic Evaluator-Optimizer (Reflexion) Loop Tests ──────────────────


@pytest.mark.asyncio
async def test_diagnostic_evaluator_optimizer_reflection_success():
    """
    Tests that when an LLM initially returns an unsupported hypothesis that is
    blocked by RCAValidator, the diagnostic harness prompts reflection,
    accepts the self-corrected hypothesis, and updates state with reflections.
    """
    state = {
        "incident_id": "inc-reflexion-01",
        "fsm_state": "collecting",
        "platform": "kubernetes",
        "target": {"name": "shop-api", "namespace": "prod", "kind": "deployment"},
        "events": [
            {
                "agent": "k8s",
                "resource_name": "shop-api",
                "namespace": "prod",
                "severity": "critical",
                "signal_type": "pod_crashloop",
            },
            {
                "agent": "git",
                "resource_name": "shop-api",
                "namespace": "prod",
                "severity": "warning",
                "signal_type": "deploy_event",
            },
        ],
        "telemetry": {"recent_logs": ["Error: bad image tag v2.1", "CrashLoopBackOff"]},
    }

    # Initial hallucinated hypothesis: claims resource_exhaustion (no OOM signals exist!)
    blocked_initial_rca = RCAResult(
        root_cause="Out of memory exhaustion",
        failure_class="resource_exhaustion",
        healing_level=1,
        runbook_id=None,
        confidence=0.85,
        reasoning="Container ran out of resources",
        source="gemini",
        suggested_action="k8s_restart_deployment",
    )

    # Refined hypothesis after validator critique: corrects to bad_deploy
    corrected_refined_rca = RCAResult(
        root_cause="Faulty deployment image rollout causing crashloop on startup",
        failure_class="bad_deploy",
        healing_level=2,
        runbook_id=None,
        confidence=0.90,
        reasoning="CrashLoopBackOff coinciding with deploy event confirms bad deploy",
        source="gemini_refined",
        suggested_action="k8s_restart_deployment",
    )

    with patch("nexus.graph.nodes.diagnose.RCAEngine") as mock_rca_cls:
        engine_instance = MagicMock()
        engine_instance._provider.is_available.return_value = True
        engine_instance.analyze = AsyncMock(return_value=blocked_initial_rca)
        engine_instance.refine = AsyncMock(return_value=corrected_refined_rca)
        mock_rca_cls.return_value = engine_instance

        result = await diagnose_node(state)

        # The engine must have been asked to refine with validator feedback
        engine_instance.refine.assert_called_once()

        diagnosis = result["diagnosis"]
        assert diagnosis["failure_class"] == "bad_deploy"
        assert diagnosis["validator_passed"] is True
        assert diagnosis["confidence"] >= 0.85

        # Check that diagnostic_reflections logged the trajectory
        reflections = result["diagnostic_reflections"]
        assert len(reflections) == 1
        assert reflections[0]["iteration"] == 1
        assert reflections[0]["outcome"] == "refined_accepted"
        assert (
            reflections[0]["initial_hypothesis"]["failure_class"]
            == "resource_exhaustion"
        )
        assert reflections[0]["refined_hypothesis"]["failure_class"] == "bad_deploy"


@pytest.mark.asyncio
async def test_diagnostic_reflection_fallback_on_unresolved_critique():
    """
    Tests that if the LLM's refined hypothesis still fails validation, the harness
    falls back cleanly to conservative downgrade without crashing.
    """
    state = {
        "incident_id": "inc-reflexion-02",
        "fsm_state": "collecting",
        "platform": "kubernetes",
        "target": {"name": "shop-api", "namespace": "prod"},
        "events": [
            {
                "agent": "k8s",
                "resource_name": "shop-api",
                "namespace": "prod",
                "severity": "warning",
                "signal_type": "high_error_rate",
            }
        ],
        "telemetry": {},
    }

    initial_rca = RCAResult(
        root_cause="Database pool exhausted",
        failure_class="resource_exhaustion",
        healing_level=2,
        runbook_id=None,
        confidence=0.80,
        reasoning="Speculative pool exhaustion",
        source="openai",
    )

    still_blocked_rca = RCAResult(
        root_cause="Upstream network failure",
        failure_class="dependency_failure",
        healing_level=1,
        runbook_id=None,
        confidence=0.80,
        reasoning="Speculative dependency failure",
        source="openai_refined",
    )

    with patch("nexus.graph.nodes.diagnose.RCAEngine") as mock_rca_cls:
        engine_instance = MagicMock()
        engine_instance._provider.is_available.return_value = True
        engine_instance.analyze = AsyncMock(return_value=initial_rca)
        engine_instance.refine = AsyncMock(return_value=still_blocked_rca)
        mock_rca_cls.return_value = engine_instance

        result = await diagnose_node(state)

        # Refined hypothesis also failed, so it fell back to unknown
        diagnosis = result["diagnosis"]
        assert diagnosis["failure_class"] == "unknown"
        reflections = result["diagnostic_reflections"]
        assert len(reflections) == 1
        assert reflections[0]["outcome"] == "refinement_rejected_fallback"


# ── 2. Verification-Driven Adaptive Recovery Ladder Tests ─────────────────────


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


@pytest.mark.asyncio
async def test_preflight_self_healing_short_circuit():
    """
    Tests that when check_live_state detects target has already self-healed,
    govern_node sets self_healed=True and resolves the incident without executing mutations.
    """
    state = {
        "incident_id": "inc-selfheal-01",
        "platform": "kubernetes",
        "target": {
            "name": "auth-service",
            "namespace": "prod",
            "platform": "kubernetes",
        },
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_restart_deployment",
                    "risk_level": "L1_SAFE_AUTOMATED",
                }
            ],
            "requires_approval": False,
            "confidence": 0.90,
        },
    }

    mock_live_report = MagicMock()
    mock_live_report.verdict = "self_healed"
    mock_live_report.evidence = "1/1 pods running and ready with restart_count=0"

    with patch(
        "nexus.graph.nodes.govern.check_live_state",
        AsyncMock(return_value=mock_live_report),
    ):
        result = await govern_node(state)

        gov = result["governance"]
        assert gov["allowed"] is False
        assert gov["self_healed"] is True
        assert result["resolved"] is True
        assert result["fsm_state"] == IncidentState.RESOLVED.value

        # Workflow router directs self-healed incidents cleanly to learn
        state_with_gov = {**state, "governance": gov, "resolved": True}
        next_route = route_after_govern(state_with_gov)
        assert next_route == "learn"


# ── 4. End-to-End Workflow with Adaptive Ladder & Human Approval ──────────────


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

    with patch(
        "nexus.agents.k8s_tools.describe_resource",
        return_value="Replicas: 1\nState: CrashLoopBackOff",
    ):
        with patch(
            "nexus.agents.k8s_tools.restart_deployment", return_value="restarted"
        ):
            with patch(
                "nexus.agents.k8s_tools.rollback_deployment", return_value="rolled_back"
            ):
                with (
                    patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05),
                    patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.01),
                ):
                    with patch.object(
                        adapter, "verify_health", side_effect=mock_health_check
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
                        assert (
                            initial_run["plan"]["steps"][0]["tool_name"]
                            == "k8s_restart_deployment"
                        )

                        # 1st resume with operator approval: Attempt 0 executes restart -> fails verify -> Attempt 1 escalates to rollback -> pauses for approval
                        escalated_run = await workflow.resume_incident(
                            thread_id="thread-adaptive-e2e",
                            approval_decision="approved",
                        )

                        assert escalated_run["fsm_state"] == "approval_pending"
                        assert escalated_run["resolved"] is False
                        assert (
                            escalated_run["plan"]["steps"][0]["tool_name"]
                            == "k8s_rollback_deployment"
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
                        executed_tools = [
                            rec["tool_name"] for rec in resumed_run["execution_records"]
                        ]
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


@pytest.mark.asyncio
async def test_k8s_telemetry_extraction_of_live_spec():
    """Verify that collect_k8s_telemetry parses structured replicas, limits, and exit codes."""
    from nexus.graph.platform.base import TargetResource
    from nexus.graph.platform.k8s_telemetry import collect_k8s_telemetry

    target = TargetResource(
        platform="kubernetes", namespace="default", name="worker-app", kind="deployment"
    )
    raw_describe = """
    Name:                   worker-app
    Namespace:              default
    Replicas:               4 desired | 4 updated | 4 total | 4 available
    Containers:
      worker-app:
        Limits:
          cpu:     1000m
          memory:  512Mi
        Requests:
          cpu:     200m
          memory:  128Mi
        Last State:     Terminated
          Reason:       OOMKilled
          Exit Code:    137
    """

    with (
        patch("nexus.agents.k8s_tools.describe_resource", return_value=raw_describe),
        patch(
            "nexus.agents.k8s_tools.get_pod_logs",
            return_value="worker started\nOOMKilled exit code 137",
        ),
    ):
        telemetry = await collect_k8s_telemetry(target)

        assert telemetry.live_config["replicas"] == 4
        assert telemetry.live_config["memory_limit"] == "512Mi"
        assert telemetry.live_config["cpu_limit"] == "1000m"
        assert telemetry.live_config["exit_code"] == 137
        assert telemetry.metrics["current_replicas"] == 4
        assert telemetry.metrics["memory_limit"] == "512Mi"
