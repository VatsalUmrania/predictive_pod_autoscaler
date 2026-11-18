"""
Unit Tests for NEXUS LangGraph Approval Interrupt, Clean Command(resume=...),
Approval Reset on Retries, and Post-Incident Report Guard.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from nexus.graph.builder import build_incident_graph
from nexus.graph.nodes.approval import approval_node
from nexus.graph.nodes.govern import govern_node
from nexus.graph.rca.result import DiscoveredTarget, RCAResult, VerificationResult
from nexus.graph.routes import route_after_approval
from nexus.graph.workflow import run_incident_workflow
from nexus.graph.workflow_approval import handle_incident_approval
from nexus.graph.workflow_completion import finalize_workflow_run


def _make_fake_llm(
    rca: RCAResult, plan_tool_calls: list[dict[str, Any]], verify: VerificationResult
):
    class _FakeStructured:
        def __init__(self, schema: Any):
            self._schema = schema

        async def ainvoke(self, messages: Any) -> Any:
            if self._schema is RCAResult:
                return rca
            if self._schema is VerificationResult:
                return verify
            raise ValueError(f"Unexpected schema {self._schema}")

    class _FakeBound:
        def __init__(self, tools: list[Any]):
            self._tool_names = {t.name for t in tools}

        async def ainvoke(self, messages: Any) -> AIMessage:
            # If mutation tools are bound, return planned mutation tool call(s)
            if any(tc["name"] in self._tool_names for tc in plan_tool_calls):
                return AIMessage(
                    content="Remediating target", tool_calls=plan_tool_calls
                )
            return AIMessage(content="Observation complete", tool_calls=[])

    class _FakeLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observation complete", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            return _FakeBound(tools)

        def with_structured_output(self, schema: Any) -> Any:
            return _FakeStructured(schema)

    return _FakeLLM()


def test_route_after_approval_logic():
    assert (
        route_after_approval({"approved": True, "approval_status": "APPROVED"})
        == "execute"
    )
    assert route_after_approval({"approval_status": "AUTO_APPROVED"}) == "execute"
    assert (
        route_after_approval({"approved": False, "approval_status": "REJECTED"})
        == "escalate"
    )
    assert route_after_approval({"approval_status": "PENDING"}) == "escalate"


@pytest.mark.asyncio
async def test_govern_node_resets_prior_approval_on_requires_approval():
    """Verify govern_node clears prior approved/approved_by/approval_reason when approval is required."""
    state = {
        "incident_id": "inc-reset-01",
        "platform": "k8s",
        "namespace": "shop-ns",
        "resource_kind": "Deployment",
        "service_name": "shop-demo",
        "confidence": 0.9,
        "approved": True,
        "approved_by": "previous-operator",
        "approval_reason": "attempt 1",
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_rollback_deployment",
                    "tool_args": {
                        "namespace": "shop-ns",
                        "deployment_name": "shop-demo",
                    },
                    "blast_radius": "service_wide",
                    "reversible": True,
                }
            ]
        },
    }
    with patch(
        "nexus.graph.nodes.govern.evaluate_opa_policy",
        AsyncMock(
            return_value={
                "allowed": True,
                "requires_approval": True,
                "reason": "Requires approval",
            }
        ),
    ):
        res = await govern_node(state)

    assert res["requires_approval"] is True
    assert res["approval_status"] == "PENDING"
    assert res["approved"] is None
    assert res["approved_by"] is None
    assert res["approval_reason"] is None
    assert res["status"] == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_approval_node_requires_both_status_and_flag():
    """approval_node short-circuits only when approval_status=='APPROVED' and approved is True."""
    res_auto = await approval_node(
        {"incident_id": "inc-1", "approval_status": "AUTO_APPROVED"}
    )
    assert res_auto["approved"] is True
    assert res_auto["status"] == "APPROVED"

    res_approved = await approval_node(
        {"incident_id": "inc-2", "approval_status": "APPROVED", "approved": True}
    )
    assert res_approved["approved"] is True
    assert res_approved["status"] == "APPROVED"

    res_rejected = await approval_node(
        {"incident_id": "inc-3", "approval_status": "REJECTED", "approved": False}
    )
    assert res_rejected["approved"] is False
    assert res_rejected["status"] == "ESCALATED"


@pytest.mark.asyncio
async def test_finalize_workflow_run_strict_report_guards():
    """Post-Incident Report MUST NOT fire while awaiting approval or with 0 executed steps."""
    with patch(
        "nexus.integration.incident_report.publish_post_incident_report",
        AsyncMock(),
    ) as mock_publish:
        await finalize_workflow_run(
            "inc-paused",
            {
                "status": "AWAITING_APPROVAL",
                "approval_status": "PENDING",
                "execution_results": [],
            },
        )
        await finalize_workflow_run(
            "inc-zero-steps",
            {
                "status": "ESCALATED",
                "approval_status": "REJECTED",
                "execution_results": [],
            },
        )
        mock_publish.assert_not_called()

        await finalize_workflow_run(
            "inc-done",
            {
                "status": "RESOLVED",
                "approval_status": "APPROVED",
                "execution_results": [
                    {
                        "step": 1,
                        "tool_name": "k8s_rollback_deployment",
                        "status": "SUCCESS",
                    }
                ],
            },
        )
        mock_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2e_interrupt_pause_no_report_then_clean_command_resume_approved():
    """
    Full pause/resume flow:
      1. run_incident_workflow pauses at approval_node (snapshot.next == ('approval',))
      2. Sends approval notification & syncs discovered target to DB; does NOT publish Post-Incident Report
      3. handle_incident_approval resumes directly via Command(resume=...) WITHOUT calling aupdate_state
      4. Executes tool -> verifies -> publishes Post-Incident Report once with 1 executed step.
    """
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="checkout-api",
        ),
        failure_type="oom_killed_memory_limit",
        root_cause_summary="Container exceeded 256Mi memory limit (exit code 137).",
        confidence=0.95,
        evidence=["lastState.terminated.exitCode=137"],
        can_auto_remediate=True,
        recommended_strategy="Patch memory limit on Deployment/checkout-api to 512Mi.",
    )
    plan_calls = [
        {
            "id": "call_1",
            "name": "k8s_patch_resource_limits",
            "args": {
                "namespace": "production",
                "deployment_name": "checkout-api",
                "cpu_limit": "500m",
                "memory_limit": "512Mi",
            },
        }
    ]
    verif = VerificationResult(
        verification_passed=True,
        observed_state_summary="Deployment checkout-api readyReplicas=2/2.",
    )
    fake_llm = _make_fake_llm(rca, plan_calls, verif)
    compiled = build_incident_graph(checkpointer=MemorySaver())

    mock_tool = MagicMock()
    mock_tool.ainvoke = AsyncMock(return_value="Patched limits for checkout-api")
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [MagicMock(name="k8s_patch_resource_limits")] if mode == "mutate" else []
    )
    mock_registry.get_tools.side_effect = None

    real_patch_tool = MagicMock()
    real_patch_tool.name = "k8s_patch_resource_limits"
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [real_patch_tool] if mode == "mutate" else []
    )
    mock_registry.get_tool.return_value = mock_tool
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="service_wide", reversible=True
    )

    mock_notify = AsyncMock()
    mock_publish = AsyncMock()
    mock_sync = AsyncMock()

    with (
        patch("nexus.graph.nodes.diagnose.get_chat_model", return_value=fake_llm),
        patch("nexus.graph.nodes.plan.get_chat_model", return_value=fake_llm),
        patch("nexus.graph.nodes.verify.get_chat_model", return_value=fake_llm),
        patch(
            "nexus.graph.nodes.diagnose.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.graph.nodes.plan.get_tool_registry", return_value=mock_registry),
        patch(
            "nexus.graph.nodes.execute.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.graph.nodes.verify.get_tool_registry", return_value=mock_registry),
        patch(
            "nexus.graph.nodes.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA approval required",
                }
            ),
        ),
        patch("nexus.graph.workflow.send_approval_request_notification", mock_notify),
        patch("nexus.graph.workflow.sync_discovered_target_identity", mock_sync),
        patch("nexus.graph.nodes.diagnose.sync_discovered_target_identity", mock_sync),
        patch("nexus.graph.nodes.approval.sync_discovered_target_identity", mock_sync),
        patch(
            "nexus.integration.incident_report.publish_post_incident_report",
            mock_publish,
        ),
    ):
        paused_state = await run_incident_workflow(
            compiled,
            {
                "incident_id": "inc-hitl-01",
                "platform": "k8s",
                "namespace": None,
                "service_name": None,
                "raw_events": [{"message": "OOMKilled"}],
            },
        )

        # 1. Paused cleanly at approval without premature Post-Incident Report
        assert paused_state["status"] == "AWAITING_APPROVAL"
        assert paused_state["target_resource"] == "production/Deployment/checkout-api"
        assert paused_state.get("execution_results", []) == []
        mock_notify.assert_awaited_once()
        mock_publish.assert_not_called()
        assert mock_sync.await_count >= 1

        # 2. Resume directly via Command(resume=...) — verify aupdate_state is NOT called
        with patch.object(
            compiled, "aupdate_state", wraps=compiled.aupdate_state
        ) as spy_update:
            final_state = await handle_incident_approval(
                compiled,
                incident_id="inc-hitl-01",
                approved=True,
                approver="alice",
                reason="Looks safe",
            )
            spy_update.assert_not_called()

        assert final_state["status"] == "RESOLVED"
        assert final_state["approved"] is True
        assert final_state["approved_by"] == "alice"
        assert final_state["execution_status"] == "SUCCESS"
        assert len(final_state["execution_results"]) == 1
        mock_tool.ainvoke.assert_awaited_once_with(plan_calls[0]["args"])
        mock_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2e_interrupt_resume_rejected_skips_post_incident_report():
    """Rejecting an approval routes to escalate with 0 executed steps and emits NO Post-Incident Report."""
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="checkout-api",
        ),
        failure_type="readiness_probe_timeout",
        root_cause_summary="Probe timed out.",
        confidence=0.9,
        evidence=["probe timeout"],
        can_auto_remediate=True,
        recommended_strategy="Restart deployment.",
    )
    plan_calls = [
        {
            "id": "call_1",
            "name": "k8s_restart_deployment",
            "args": {"namespace": "production", "deployment_name": "checkout-api"},
        }
    ]
    verif = VerificationResult(verification_passed=True, observed_state_summary="OK")
    fake_llm = _make_fake_llm(rca, plan_calls, verif)
    compiled = build_incident_graph(checkpointer=MemorySaver())

    real_tool = MagicMock()
    real_tool.name = "k8s_restart_deployment"
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [real_tool] if mode == "mutate" else []
    )
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="pod", reversible=True
    )
    mock_publish = AsyncMock()

    with (
        patch("nexus.graph.nodes.diagnose.get_chat_model", return_value=fake_llm),
        patch("nexus.graph.nodes.plan.get_chat_model", return_value=fake_llm),
        patch(
            "nexus.graph.nodes.diagnose.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.graph.nodes.plan.get_tool_registry", return_value=mock_registry),
        patch(
            "nexus.graph.nodes.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch("nexus.graph.workflow.send_approval_request_notification", AsyncMock()),
        patch(
            "nexus.integration.incident_report.publish_post_incident_report",
            mock_publish,
        ),
    ):
        await run_incident_workflow(
            compiled,
            {
                "incident_id": "inc-hitl-reject",
                "platform": "k8s",
                "raw_events": [{"message": "Readiness probe failed"}],
            },
        )
        final_state = await handle_incident_approval(
            compiled,
            incident_id="inc-hitl-reject",
            approved=False,
            approver="bob",
            reason="Maintenance window active",
        )

    assert final_state["status"] == "ESCALATED"
    assert final_state["approved"] is False
    assert final_state.get("execution_results", []) == []
    mock_publish.assert_not_called()


@pytest.mark.asyncio
async def test_incident_workflow_with_nats_dispatches_single_approval_notification():
    """With NATS connected, IncidentWorkflow dispatches approval once over NATS and skips direct Slack post."""
    from nexus.graph.checkpointer import GraphCheckpointStore
    from nexus.graph.workflow import IncidentWorkflow

    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="default",
            resource_kind="Deployment",
            resource_name="shop-demo",
        ),
        failure_type="container_command_misconfiguration",
        root_cause_summary="Container command override exits immediately with code 1.",
        confidence=1.0,
        evidence=["command=['/bin/sh', '-c', 'exit 1']"],
        can_auto_remediate=True,
        recommended_strategy="Remove erroneous command override from shop-demo.",
    )
    plan_calls = [
        {
            "id": "call_1",
            "name": "k8s_remove_container_command",
            "args": {"namespace": "default", "deployment_name": "shop-demo"},
        }
    ]
    verif = VerificationResult(verification_passed=True, observed_state_summary="OK")
    fake_llm = _make_fake_llm(rca, plan_calls, verif)

    real_tool = MagicMock()
    real_tool.name = "k8s_remove_container_command"
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [real_tool] if mode == "mutate" else []
    )
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="service_wide", reversible=True
    )

    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_direct_notify = AsyncMock()

    wf = IncidentWorkflow(
        checkpointer_store=GraphCheckpointStore(MemorySaver()),
        nats_client=mock_nats,
    )

    with (
        patch("nexus.graph.nodes.diagnose.get_chat_model", return_value=fake_llm),
        patch("nexus.graph.nodes.plan.get_chat_model", return_value=fake_llm),
        patch("nexus.graph.nodes.diagnose.get_tool_registry", return_value=mock_registry),
        patch("nexus.graph.nodes.plan.get_tool_registry", return_value=mock_registry),
        patch(
            "nexus.graph.nodes.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "Approval required by OPA policy",
                }
            ),
        ),
        patch("nexus.graph.workflow.send_approval_request_notification", mock_direct_notify),
        patch("nexus.graph.workflow.sync_discovered_target_identity", AsyncMock()),
        patch("nexus.graph.nodes.diagnose.sync_discovered_target_identity", AsyncMock()),
        patch("nexus.graph.nodes.approval.sync_discovered_target_identity", AsyncMock()),
    ):
        paused = await wf.run_incident(
            {
                "incident_id": "CL-SINGLE-APPR",
                "platform": "k8s",
                "raw_events": [{"message": "CrashLoopBackOff"}],
            },
            thread_id="CL-SINGLE-APPR",
        )

    assert paused["status"] == "AWAITING_APPROVAL"
    # Direct Slack dispatch is skipped when NATS publishes nexus.approvals.required
    mock_direct_notify.assert_not_awaited()
    mock_nats.publish_raw.assert_awaited_once()
    assert mock_nats.publish_raw.await_args.args[0] == "nexus.approvals.required"
