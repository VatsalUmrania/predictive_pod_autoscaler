"""
Unit Tests for Phase 4: Planning & Verification Contracts
=========================================================
Tests safe verification condition evaluation, operator security, metric extraction,
autonomous LLM planning with deterministic fallback, and verify_node integration.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from nexus.graph.nodes.plan import plan_remediation_node
from nexus.graph.nodes.verify import (
    evaluate_verification_condition,
    extract_metric_value,
    verify_node,
)
from nexus.graph.platform import HealthCheckResult
from nexus.graph.state import VerificationCondition

# ─────────────────────────────────────────────────────────────────────────────
# 1. Verification Condition Evaluation Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_verification_condition_operators():
    """Verify all supported comparison operators evaluate correctly without eval()."""
    # Less than (<)
    cond_lt = VerificationCondition(
        metric_name="error_rate", operator="<", threshold=0.05
    )
    passed, msg = evaluate_verification_condition(cond_lt, 0.02)
    assert passed is True
    assert "PASS" in msg

    passed, msg = evaluate_verification_condition(cond_lt, 0.08)
    assert passed is False
    assert "FAIL" in msg

    # Less than or equal (<=)
    cond_le = VerificationCondition(
        metric_name="memory_usage_pct", operator="<=", threshold=85.0
    )
    passed, _ = evaluate_verification_condition(cond_le, 85.0)
    assert passed is True
    passed, _ = evaluate_verification_condition(cond_le, 85.1)
    assert passed is False

    # Greater than (>)
    cond_gt = VerificationCondition(
        metric_name="throughput", operator=">", threshold=100.0
    )
    passed, _ = evaluate_verification_condition(cond_gt, 150.0)
    assert passed is True
    passed, _ = evaluate_verification_condition(cond_gt, 50.0)
    assert passed is False

    # Greater than or equal (>=)
    cond_ge = VerificationCondition(
        metric_name="ready_replicas", operator=">=", threshold=1.0
    )
    passed, _ = evaluate_verification_condition(cond_ge, 1.0)
    assert passed is True
    passed, _ = evaluate_verification_condition(cond_ge, 0.0)
    assert passed is False

    # Equal (==)
    cond_eq = VerificationCondition(metric_name="Errors", operator="==", threshold=0.0)
    passed, _ = evaluate_verification_condition(cond_eq, 0.0)
    assert passed is True
    passed, _ = evaluate_verification_condition(cond_eq, 1.0)
    assert passed is False

    # Not equal (!=)
    cond_ne = VerificationCondition(
        metric_name="exit_code", operator="!=", threshold=137.0
    )
    passed, _ = evaluate_verification_condition(cond_ne, 0.0)
    assert passed is True
    passed, _ = evaluate_verification_condition(cond_ne, 137.0)
    assert passed is False


def test_evaluate_verification_condition_malicious_operator_rejection():
    """Verify malicious or arbitrary strings cannot execute or evaluate."""
    for bad_op in (
        "eval",
        "__import__('os').system('ls')",
        "exec",
        "system",
        "or 1==1",
        ";",
    ):
        cond = {"metric_name": "cpu", "operator": bad_op, "threshold": 50.0}
        passed, msg = evaluate_verification_condition(cond, 10.0)
        assert passed is False
        assert "Unsupported or dangerous comparison operator" in msg


def test_evaluate_verification_condition_invalid_numeric():
    """Verify non-numeric values fail safely without unhandled exceptions."""
    cond = VerificationCondition(metric_name="cpu", operator="<", threshold=50.0)
    passed, msg = evaluate_verification_condition(cond, "not-a-number")
    assert passed is False
    assert "invalid observed value" in msg


# ─────────────────────────────────────────────────────────────────────────────
# 2. Metric Extraction Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_extract_metric_value_sources():
    """Verify numeric metric values can be extracted from multiple telemetry sources."""
    # From tool_data direct
    val = extract_metric_value("ready_replicas", tool_data={"ready_replicas": 3})
    assert val == 3.0

    # From tool_data nested metrics
    val = extract_metric_value(
        "memory_usage_pct", tool_data={"metrics": {"memory_usage_pct": 74.5}}
    )
    assert val == 74.5

    # From CloudWatch datapoints
    tool_data_cw = {"datapoints": [{"Average": 12.4}, {"Average": 0.0}]}
    val = extract_metric_value("Errors", tool_data=tool_data_cw)
    assert val == 0.0

    # From k8s describe status regex
    val = extract_metric_value(
        "ready_replicas", tool_data={"status": "Deployment status: 2 ready, 2 total"}
    )
    assert val == 2.0

    # From health_result metrics
    mock_health = HealthCheckResult(
        healthy=True, slo_restored=True, metrics={"Errors": 0.0}
    )
    val = extract_metric_value("Errors", health_result=mock_health)
    assert val == 0.0

    # From telemetry metrics
    telemetry = {"metrics": {"cpu_utilization": 42.0}}
    val = extract_metric_value("cpu_utilization", telemetry=telemetry)
    assert val == 42.0

    # None when missing
    assert extract_metric_value("nonexistent", tool_data={}) is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Planning Node Tests (LLM & Fallback)
# ─────────────────────────────────────────────────────────────────────────────


class MockPlanChatModel(BaseChatModel):
    """Mock LangChain chat model returning predetermined structured plan."""

    response_text: str

    def __init__(self, response_text: str, **kwargs: Any):
        super().__init__(response_text=response_text, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "mock-plan-chat-model"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )


def test_plan_node_deterministic_when_fallback_active():
    """Verify that plan_remediation_node uses deterministic planner when fallback_active is True."""
    state = {
        "incident_id": "inc-fallback-plan-01",
        "platform": "kubernetes",
        "fallback_active": True,
        "target": {"name": "order-service", "namespace": "prod"},
        "diagnosis": {
            "failure_class": "bad_deploy",
            "root_cause": "Rollout regression with crashlooping containers",
            "confidence": 0.9,
        },
        "telemetry": {},
        "retry_count": 0,
    }

    result = plan_remediation_node(state)
    plan = result["plan"]

    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_rollback_deployment"
    assert step["verification_tool"] == "k8s_describe_resource"
    assert step["verification_condition"] is not None
    assert step["verification_condition"]["metric_name"] == "ready_replicas"
    assert step["verification_condition"]["operator"] == ">="
    assert step["verification_condition"]["threshold"] == 1.0


def test_plan_node_autonomous_llm_success():
    """Verify that plan_remediation_node successfully parses an autonomous LLM plan."""
    llm_plan_json = json.dumps(
        {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_scale_resource",
                    "parameters": {
                        "namespace": "prod",
                        "resource_name": "checkout-svc",
                        "resource_kind": "deployment",
                        "replicas": 4,
                    },
                    "description": "Scale checkout-svc to 4 replicas to handle burst traffic",
                    "rollback_tool": "k8s_scale_resource",
                    "rollback_parameters": {
                        "namespace": "prod",
                        "resource_name": "checkout-svc",
                        "resource_kind": "deployment",
                        "replicas": 2,
                    },
                    "verification_tool": "k8s_describe_resource",
                    "verification_parameters": {
                        "namespace": "prod",
                        "resource_kind": "deployment",
                        "resource_name": "checkout-svc",
                    },
                    "verification_condition": {
                        "metric_name": "ready_replicas",
                        "operator": ">=",
                        "threshold": 4.0,
                        "window_seconds": 60,
                    },
                }
            ],
            "requires_approval": True,
            "approval_reason": "Scaling production deployment checkout-svc to 4 replicas",
            "expected_slo_target": "p99_latency < 200ms",
        }
    )

    mock_llm = MockPlanChatModel(response_text=f"```json\n{llm_plan_json}\n```")

    state = {
        "incident_id": "inc-llm-plan-01",
        "platform": "kubernetes",
        "fallback_active": False,
        "target": {"name": "checkout-svc", "namespace": "prod"},
        "diagnosis": {
            "failure_class": "cascading_failure",
            "root_cause": "Spike in incoming checkout requests causing queue backlog",
            "confidence": 0.95,
        },
        "telemetry": {},
        "retry_count": 0,
    }

    with patch("nexus.graph.nodes.plan.get_llm", return_value=mock_llm):
        result = plan_remediation_node(state)

    plan = result["plan"]
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["tool_name"] == "k8s_scale_resource"
    assert step["parameters"]["replicas"] == 4
    assert step["rollback_tool"] == "k8s_scale_resource"
    assert step["verification_tool"] == "k8s_describe_resource"
    assert step["verification_condition"]["metric_name"] == "ready_replicas"
    assert step["verification_condition"]["threshold"] == 4.0
    assert plan["expected_slo_target"] == "p99_latency < 200ms"


def test_plan_node_autonomous_llm_failure_falls_back():
    """Verify that plan_remediation_node falls back to deterministic planning on LLM exception."""
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = RuntimeError("Connection timed out to LLM provider")

    state = {
        "incident_id": "inc-llm-fail-01",
        "platform": "kubernetes",
        "fallback_active": False,
        "target": {"name": "auth-service", "namespace": "default"},
        "diagnosis": {
            "failure_class": "bad_deploy",
            "root_cause": "Broken deployment revision",
            "confidence": 0.85,
        },
        "telemetry": {},
        "retry_count": 0,
    }

    with patch("nexus.graph.nodes.plan.get_llm", return_value=mock_llm):
        result = plan_remediation_node(state)

    plan = result["plan"]
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["tool_name"] == "k8s_rollback_deployment"
    assert plan["steps"][0]["verification_condition"] is not None


def test_plan_node_autonomous_llm_unregistered_tool_falls_back():
    """Verify that an LLM hallucinating unregistered tools is rejected and falls back."""
    llm_bad_plan = json.dumps(
        {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "malicious_unregistered_tool_drop_db",
                    "parameters": {},
                }
            ]
        }
    )
    mock_llm = MockPlanChatModel(response_text=llm_bad_plan)

    state = {
        "incident_id": "inc-bad-tool-01",
        "platform": "kubernetes",
        "fallback_active": False,
        "target": {"name": "auth-service", "namespace": "default"},
        "diagnosis": {
            "failure_class": "bad_deploy",
            "root_cause": "Broken deployment revision",
            "confidence": 0.85,
        },
        "telemetry": {},
        "retry_count": 0,
    }

    with patch("nexus.graph.nodes.plan.get_llm", return_value=mock_llm):
        result = plan_remediation_node(state)

    # Hallucinated tool rejected -> deterministic fallback used
    plan = result["plan"]
    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["tool_name"] == "k8s_rollback_deployment"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Verify Node Integration Tests
# ─────────────────────────────────────────────────────────────────────────────


def _fake_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = adapter
    registry.resolve_adapter.return_value = adapter
    return registry


@pytest.mark.asyncio
async def test_verify_node_condition_passed():
    """Verify that verify_node marks resolved when condition evaluates to PASS."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        return_value=HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details="Deployment healthy",
            metrics={"ready_replicas": 2.0},
        )
    )

    state = {
        "incident_id": "inc-verif-pass-01",
        "platform": "kubernetes",
        "target": {"name": "app-svc", "namespace": "prod"},
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_restart_deployment",
                    "verification_condition": {
                        "metric_name": "ready_replicas",
                        "operator": ">=",
                        "threshold": 1.0,
                        "window_seconds": 60,
                    },
                }
            ]
        },
        "retry_count": 0,
        "max_retries": 2,
    }

    with (
        patch(
            "nexus.graph.nodes.verify.get_platform_registry",
            return_value=_fake_registry(adapter),
        ),
        patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 1.0),
        patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.01),
    ):
        result = await verify_node(state)

    assert result["resolved"] is True
    assert result["verification"]["healthy"] is True
    assert result["fsm_state"] == "resolved"
    assert "PASS" in result["verification"]["details"]


@pytest.mark.asyncio
async def test_verify_node_condition_failed_triggers_retry():
    """Verify that verify_node triggers retry when VerificationCondition fails despite adapter health."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        return_value=HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details="Deployment running",
            metrics={"ready_replicas": 1.0},
        )
    )

    state = {
        "incident_id": "inc-verif-fail-01",
        "platform": "kubernetes",
        "target": {"name": "app-svc", "namespace": "prod"},
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_scale_resource",
                    # Condition requires 3 replicas, but observed is only 1.0
                    "verification_condition": {
                        "metric_name": "ready_replicas",
                        "operator": ">=",
                        "threshold": 3.0,
                        "window_seconds": 60,
                    },
                }
            ]
        },
        "retry_count": 0,
        "max_retries": 2,
    }

    with (
        patch(
            "nexus.graph.nodes.verify.get_platform_registry",
            return_value=_fake_registry(adapter),
        ),
        patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05),
        patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.02),
    ):
        result = await verify_node(state)

    assert result.get("resolved") is not True
    assert result["verification"]["healthy"] is False
    assert result["fsm_state"] == "retrying"
    assert result["retry_count"] == 1
    assert "FAIL" in result["verification"]["failure_reason"]


@pytest.mark.asyncio
async def test_verify_node_condition_failed_triggers_rollback_on_max_retries():
    """Verify that verify_node transitions to ROLLING_BACK when max retries are exceeded."""
    adapter = MagicMock()
    adapter.verify_health = AsyncMock(
        return_value=HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details="Deployment running",
            metrics={"ready_replicas": 1.0},
        )
    )

    state = {
        "incident_id": "inc-verif-rollback-01",
        "platform": "kubernetes",
        "target": {"name": "app-svc", "namespace": "prod"},
        "plan": {
            "steps": [
                {
                    "step_index": 0,
                    "tool_name": "k8s_scale_resource",
                    "verification_condition": {
                        "metric_name": "ready_replicas",
                        "operator": ">=",
                        "threshold": 3.0,
                        "window_seconds": 60,
                    },
                }
            ]
        },
        "retry_count": 2,
        "max_retries": 2,  # Already at max retries
    }

    with (
        patch(
            "nexus.graph.nodes.verify.get_platform_registry",
            return_value=_fake_registry(adapter),
        ),
        patch("nexus.graph.nodes.verify.VERIFY_TIMEOUT_S", 0.05),
        patch("nexus.graph.nodes.verify.VERIFY_POLL_INTERVAL_S", 0.02),
    ):
        result = await verify_node(state)

    assert result.get("resolved") is not True
    assert result["verification"]["healthy"] is False
    assert result["fsm_state"] == "rolling_back"
