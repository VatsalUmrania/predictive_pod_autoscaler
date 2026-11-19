"""
Comprehensive tests for NEXUS LangGraph Zero-Hardcoding Agentic Incident Workflow.
Covers:
  - Kubernetes HITL approval & resume (OOM -> Pause -> Approve -> Execute -> Verify -> Resolved)
  - AWS HITL approval & resume (Lambda OOM -> Pause -> Approve -> Execute -> Verify -> Resolved)
  - Human operator rejection pathway (Pause -> Reject -> Escalate with 0 executed steps)
  - Verification failure exhausting max_remediation_attempts -> Rollback -> Escalate
  - Governed lifecycle graph topology verification (all 10 nodes present)
  - Auto-approved dynamic tool planning without static runbooks
  - IncidentWorkflow pending_approvals() & Status API integration
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from nexus.engine.reasoning.rca.result import (
    DiscoveredTarget,
    RCAResult,
    VerificationResult,
)
from nexus.engine.workflow.graph.workflow import IncidentWorkflow


def _build_workflow_llm(
    rca: RCAResult,
    plan_tool_calls: list[dict[str, Any]],
    verification: VerificationResult,
) -> Any:
    class _WorkflowLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observations complete.", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            bound_names = {t.name for t in tools}

            class _Bound:
                async def ainvoke(self, messages: Any) -> AIMessage:
                    if any(tc["name"] in bound_names for tc in plan_tool_calls):
                        return AIMessage(
                            content="Generated remediation plan.",
                            tool_calls=plan_tool_calls,
                        )
                    return AIMessage(content="Observations complete.", tool_calls=[])

            return _Bound()

        def with_structured_output(self, schema: Any) -> Any:
            class _Struct:
                async def ainvoke(self, messages: Any) -> Any:
                    if schema is RCAResult:
                        return rca
                    if schema is VerificationResult:
                        return verification
                    raise ValueError(schema)

            return _Struct()

    return _WorkflowLLM()


def _mock_registry_for_tool(
    tool_name: str, output: str = "OK", blast_radius: str = "service_wide"
):
    tool_fn = MagicMock()
    tool_fn.name = tool_name
    tool_fn.ainvoke = AsyncMock(return_value=output)

    reg = MagicMock()
    reg.get_tools.side_effect = lambda mode, platform=None: (
        [tool_fn] if mode == "mutate" else []
    )
    reg.get_tool.side_effect = lambda name: tool_fn if name == tool_name else None
    reg.get.return_value = None
    reg.get_metadata.return_value = SimpleNamespace(
        blast_radius=blast_radius, reversible=True
    )
    return reg, tool_fn


@pytest.mark.asyncio
async def test_k8s_remediation_requires_approval_and_resumes():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="checkout-api",
        ),
        failure_type="oom_killed_container",
        root_cause_summary="OOMKilled container memory saturation.",
        confidence=0.95,
        evidence=["exitCode=137"],
        can_auto_remediate=True,
        recommended_strategy="Restart Deployment/checkout-api.",
    )
    calls = [
        {
            "id": "k8s_1",
            "name": "k8s_restart_deployment",
            "args": {"namespace": "production", "deployment_name": "checkout-api"},
        }
    ]
    verif = VerificationResult(
        verification_passed=True, observed_state_summary="Pods Ready"
    )
    llm = _build_workflow_llm(rca, calls, verif)
    reg, tool_fn = _mock_registry_for_tool(
        "k8s_restart_deployment", "Restarted checkout-api"
    )

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.execution.verify.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.execution.execute.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.execution.verify.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.graph.workflow.send_approval_request_notification",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ),
    ):
        paused = await workflow.run_incident(
            {
                "incident_id": "inc-k8s-01",
                "platform": "k8s",
                "raw_events": [{"message": "OOMKilled"}],
            },
            thread_id="inc-k8s-01",
        )
        assert paused["status"] == "AWAITING_APPROVAL"
        assert workflow.has_pending("inc-k8s-01") is True

        resumed = await workflow.resume_incident("inc-k8s-01", "approved")
        assert resumed["status"] == "RESOLVED"
        assert resumed["execution_status"] == "SUCCESS"
        tool_fn.ainvoke.assert_awaited_once_with(calls[0]["args"])


@pytest.mark.asyncio
async def test_aws_hitl_approval_and_resume():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="aws",
            namespace_or_region="us-east-1",
            resource_kind="Lambda",
            resource_name="payment-worker",
        ),
        failure_type="lambda_out_of_memory",
        root_cause_summary="Lambda exceeded 256MB memory allocation.",
        confidence=0.93,
        evidence=["Runtime.OutOfMemory"],
        can_auto_remediate=True,
        recommended_strategy="Increase Lambda memory to 512MB.",
    )
    calls = [
        {
            "id": "aws_1",
            "name": "aws_update_lambda_memory",
            "args": {
                "function_name": "payment-worker",
                "memory_mb": 512,
                "region": "us-east-1",
            },
        }
    ]
    verif = VerificationResult(
        verification_passed=True, observed_state_summary="0 Lambda errors"
    )
    llm = _build_workflow_llm(rca, calls, verif)
    reg, tool_fn = _mock_registry_for_tool("aws_update_lambda_memory", "MemorySize=512")

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.execution.verify.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.execution.execute.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.execution.verify.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.graph.workflow.send_approval_request_notification",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ),
    ):
        paused = await workflow.run_incident(
            {
                "incident_id": "inc-aws-01",
                "platform": "aws",
                "raw_events": [{"message": "Lambda OOM"}],
            },
            thread_id="inc-aws-01",
        )
        assert paused["status"] == "AWAITING_APPROVAL"
        assert paused["target_resource"] == "us-east-1/Lambda/payment-worker"

        resumed = await workflow.resume_incident("inc-aws-01", "approved")
        assert resumed["status"] == "RESOLVED"
        tool_fn.ainvoke.assert_awaited_once_with(calls[0]["args"])


@pytest.mark.asyncio
async def test_operator_rejection_pathway():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="aws",
            namespace_or_region="us-east-1",
            resource_kind="Lambda",
            resource_name="billing-fn",
        ),
        failure_type="lambda_timeout",
        root_cause_summary="Lambda timed out.",
        confidence=0.9,
        evidence=["Task timed out"],
        can_auto_remediate=True,
        recommended_strategy="Increase timeout.",
    )
    calls = [
        {
            "id": "aws_to_1",
            "name": "aws_update_lambda_timeout",
            "args": {"function_name": "billing-fn", "timeout_seconds": 60},
        }
    ]
    verif = VerificationResult(verification_passed=True, observed_state_summary="OK")
    llm = _build_workflow_llm(rca, calls, verif)
    reg, tool_fn = _mock_registry_for_tool("aws_update_lambda_timeout")

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.graph.workflow.send_approval_request_notification",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ) as mock_pub,
    ):
        await workflow.run_incident(
            {
                "incident_id": "inc-rej-01",
                "platform": "aws",
                "raw_events": [{"message": "Timeout"}],
            },
            thread_id="inc-rej-01",
        )
        resumed = await workflow.resume_incident("inc-rej-01", "rejected")

    assert resumed["status"] == "ESCALATED"
    assert resumed["approved"] is False
    tool_fn.ainvoke.assert_not_called()
    mock_pub.assert_not_called()


@pytest.mark.asyncio
async def test_verification_failure_and_deterministic_rollback():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="search-api",
        ),
        failure_type="crashloop_post_deploy",
        root_cause_summary="CrashLoopBackOff persists.",
        confidence=0.91,
        evidence=["CrashLoopBackOff"],
        can_auto_remediate=True,
        recommended_strategy="Restart search-api.",
    )
    calls = [
        {
            "id": "k8s_rb_1",
            "name": "k8s_restart_deployment",
            "args": {"namespace": "production", "deployment_name": "search-api"},
        }
    ]
    verif = VerificationResult(
        verification_passed=False,
        observed_state_summary="0/2 ready",
        failure_feedback="Still crashing",
    )
    llm = _build_workflow_llm(rca, calls, verif)
    reg, _ = _mock_registry_for_tool("k8s_restart_deployment")

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.execution.verify.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.execution.execute.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.execution.verify.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.execution.rollback.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": False,
                    "reason": "Auto",
                }
            ),
        ),
        patch(
            "nexus.services.observability.dashboard.dashboard.record_incident_resolution",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ),
    ):
        final = await workflow.run_incident(
            {
                "incident_id": "inc-rb-01",
                "platform": "k8s",
                "max_remediation_attempts": 1,
                "raw_events": [{"message": "CrashLoopBackOff"}],
            },
            thread_id="inc-rb-01",
        )

    assert final["status"] == "ESCALATED"
    assert final["verification_passed"] is False
    assert len(final["rollback_results"]) == 1


def test_governed_zero_hardcoding_graph_topology():
    """Verify compiled StateGraph contains all 10 governed lifecycle nodes."""
    workflow = IncidentWorkflow()
    graph = workflow._compiled_graph

    expected_nodes = {
        "triage",
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


@pytest.mark.asyncio
async def test_dynamic_tool_planning_without_runbooks_and_auto_approval():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="shop-ns",
            resource_kind="Deployment",
            resource_name="shop-demo",
        ),
        failure_type="transient_deadlock_probe_failure",
        root_cause_summary="Transient deadlock blocking /health.",
        confidence=0.97,
        evidence=["readiness probe timeout"],
        can_auto_remediate=True,
        recommended_strategy="Rolling restart shop-demo.",
    )
    calls = [
        {
            "id": "auto_1",
            "name": "k8s_restart_deployment",
            "args": {"namespace": "shop-ns", "deployment_name": "shop-demo"},
        }
    ]
    verif = VerificationResult(
        verification_passed=True, observed_state_summary="2/2 ready"
    )
    llm = _build_workflow_llm(rca, calls, verif)
    reg, tool_fn = _mock_registry_for_tool("k8s_restart_deployment", blast_radius="pod")

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.execution.verify.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.execution.execute.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.execution.verify.get_tool_registry", return_value=reg
        ),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": False,
                    "reason": "Auto",
                }
            ),
        ),
        patch(
            "nexus.services.observability.dashboard.dashboard.record_incident_resolution",
            AsyncMock(),
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            AsyncMock(),
        ),
    ):
        final = await workflow.run_incident(
            {
                "incident_id": "inc-dyn-01",
                "platform": "k8s",
                "raw_events": [{"message": "Unhealthy"}],
            },
            thread_id="inc-dyn-01",
        )

    assert final["status"] == "RESOLVED"
    assert final["approval_status"] == "AUTO_APPROVED"
    tool_fn.ainvoke.assert_awaited_once_with(calls[0]["args"])


@pytest.mark.asyncio
async def test_workflow_pending_approvals_and_status_api_integration():
    workflow = IncidentWorkflow()
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="shop-ns",
            resource_kind="Deployment",
            resource_name="shop-demo",
        ),
        failure_type="bad_config_rollout",
        root_cause_summary="Bad rollout causing 500s.",
        confidence=0.92,
        evidence=["HTTP 500"],
        can_auto_remediate=True,
        recommended_strategy="Rollback shop-demo.",
    )
    calls = [
        {
            "id": "pend_1",
            "name": "k8s_rollback_deployment",
            "args": {"namespace": "shop-ns", "deployment_name": "shop-demo"},
        }
    ]
    verif = VerificationResult(verification_passed=True, observed_state_summary="OK")
    llm = _build_workflow_llm(rca, calls, verif)
    reg, _ = _mock_registry_for_tool("k8s_rollback_deployment")

    with (
        patch("nexus.engine.nodes.analysis.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.analysis.diagnose.get_tool_registry", return_value=reg
        ),
        patch("nexus.engine.nodes.planning.plan.get_tool_registry", return_value=reg),
        patch(
            "nexus.engine.nodes.supervision.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.graph.workflow.send_approval_request_notification",
            AsyncMock(),
        ),
    ):
        await workflow.run_incident(
            {
                "incident_id": "inc-pend-01",
                "platform": "k8s",
                "raw_events": [{"message": "500s"}],
            },
            thread_id="inc-pend-01",
        )

    pending = workflow.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["incident_id"] == "inc-pend-01"
    assert pending[0]["target"] == "shop-ns/Deployment/shop-demo"
    assert pending[0]["action_type"] == "k8s_rollback_deployment"
    assert workflow.status["pending_approvals"] == 1
