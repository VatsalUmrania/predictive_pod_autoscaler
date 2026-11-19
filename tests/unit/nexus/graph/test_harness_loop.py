"""
NEXUS ReAct Diagnosis, Open Failure Classification, & Closed-Loop Verification Retry Tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from nexus.engine.nodes.diagnose import diagnose_node
from nexus.engine.reasoning.rca.result import (
    DiscoveredTarget,
    RCAResult,
    VerificationResult,
)
from nexus.engine.workflow.builder import build_incident_graph
from nexus.engine.workflow.routes import route_after_diagnose, route_after_verify
from nexus.engine.workflow.workflow import run_incident_workflow
from nexus.engine.workflow.workflow_approval import handle_incident_approval


def test_route_after_diagnose_uses_confidence_and_can_auto_remediate_not_enums():
    """Open failure slugs never force escalation when can_auto_remediate=True and confidence >= threshold."""
    assert (
        route_after_diagnose(
            {
                "failure_class": "readiness_probe_container_exit_zero",
                "confidence": 0.91,
                "min_confidence_threshold": 0.65,
                "rca_result": {"can_auto_remediate": True, "confidence": 0.91},
            }
        )
        == "plan"
    )
    assert (
        route_after_diagnose(
            {
                "failure_class": "readiness_probe_container_exit_zero",
                "confidence": 0.40,
                "min_confidence_threshold": 0.65,
                "rca_result": {"can_auto_remediate": True, "confidence": 0.40},
            }
        )
        == "escalate"
    )
    assert (
        route_after_diagnose(
            {
                "failure_class": "manual_data_migration_required",
                "confidence": 0.95,
                "rca_result": {"can_auto_remediate": False, "confidence": 0.95},
            }
        )
        == "escalate"
    )


def test_route_after_verify_closed_loop_and_max_attempts():
    assert route_after_verify({"verification_passed": True}) == "learn"
    assert (
        route_after_verify(
            {
                "verification_passed": False,
                "remediation_attempts": 1,
                "max_remediation_attempts": 2,
            }
        )
        == "diagnose"
    )
    assert (
        route_after_verify(
            {
                "verification_passed": False,
                "remediation_attempts": 2,
                "max_remediation_attempts": 2,
                "rollback_available": True,
            }
        )
        == "rollback"
    )
    assert (
        route_after_verify(
            {
                "verification_passed": False,
                "remediation_attempts": 2,
                "max_remediation_attempts": 2,
                "rollback_available": False,
            }
        )
        == "escalate"
    )


@pytest.mark.asyncio
async def test_diagnose_node_react_discovers_namespace_and_owner_from_none():
    """diagnose_node runs ReAct tool calls and populates discovered topology + open failure_class."""
    locate_tool = MagicMock()
    locate_tool.name = "locate_pod_and_owner"
    locate_tool.ainvoke = AsyncMock(
        return_value={
            "pod_name": "shop-demo-59bd7ddcf9-t2skj",
            "namespace": "shop-ns",
            "owner_kind": "Deployment",
            "owner_name": "shop-demo",
            "container_statuses": [
                {"last_terminated_state": {"terminated": {"exit_code": 0}}}
            ],
        }
    )
    mock_registry = MagicMock()
    mock_registry.get_tools.return_value = [locate_tool]

    bound_llm = MagicMock()
    bound_llm.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "d_1",
                        "name": "locate_pod_and_owner",
                        "args": {"pod_name": "shop-demo-59bd7ddcf9-t2skj"},
                    }
                ],
            ),
            AIMessage(content="Diagnosis ready.", tool_calls=[]),
        ]
    )
    structured_llm = MagicMock()
    structured_llm.ainvoke = AsyncMock(
        return_value=RCAResult(
            target=DiscoveredTarget(
                platform="k8s",
                namespace_or_region="shop-ns",
                resource_kind="Deployment",
                resource_name="shop-demo",
            ),
            failure_type="readiness_probe_container_exit_zero",
            root_cause_summary="Container exited with code 0 before serving port 8001.",
            confidence=0.94,
            evidence=["exit_code=0", "owner=Deployment/shop-demo"],
            can_auto_remediate=True,
            recommended_strategy="Remove erroneous container command override on shop-demo.",
        )
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = bound_llm
    mock_llm.with_structured_output.return_value = structured_llm

    with (
        patch(
            "nexus.engine.nodes.diagnose.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.engine.nodes.diagnose.get_chat_model", return_value=mock_llm),
        patch(
            "nexus.engine.nodes.diagnose.sync_discovered_target_identity", AsyncMock()
        ) as mock_sync,
    ):
        result = await diagnose_node(
            {
                "incident_id": "inc-diag-01",
                "platform": "k8s",
                "namespace": None,
                "service_name": None,
                "raw_events": [{"involved_object_name": "shop-demo-59bd7ddcf9-t2skj"}],
            }
        )

    locate_tool.ainvoke.assert_awaited_once_with(
        {"pod_name": "shop-demo-59bd7ddcf9-t2skj"}
    )
    synth_msgs = structured_llm.ainvoke.await_args.args[0]
    assert len(synth_msgs) == 2
    assert synth_msgs[0].type == "system"
    assert synth_msgs[1].type == "human"
    assert "shop-demo" in synth_msgs[1].content
    assert result["namespace"] == "shop-ns"
    assert result["resource_kind"] == "Deployment"
    assert result["service_name"] == "shop-demo"
    assert result["target_resource"] == "shop-ns/Deployment/shop-demo"
    assert result["failure_class"] == "readiness_probe_container_exit_zero"
    assert result["confidence"] == 0.94
    assert result["status"] == "DIAGNOSED"
    mock_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2e_closed_loop_verify_failure_retries_diagnose_and_re_interrupts_approval():
    """
    End-to-end closed-loop test:
      Attempt 1: diagnose -> plan (restart) -> govern -> approval pause -> approve -> execute -> verify FAILS
      Loop back: diagnose -> plan (rollback) -> govern (resets approved=None!) -> approval pause AGAIN
      Attempt 2: approve -> execute -> verify PASSES -> learn -> RESOLVED + Post-Incident Report published.
    """
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="checkout-web",
        ),
        failure_type="crash_loop_bad_release",
        root_cause_summary="CrashLoopBackOff after bad deployment.",
        confidence=0.92,
        evidence=["BackOff restarting failed container"],
        can_auto_remediate=True,
        recommended_strategy="Restart or rollback Deployment/checkout-web.",
    )

    plan_attempts = [
        AIMessage(
            content="Attempt 1: restart deployment",
            tool_calls=[
                {
                    "id": "call_attempt_1",
                    "name": "k8s_restart_deployment",
                    "args": {
                        "namespace": "production",
                        "deployment_name": "checkout-web",
                    },
                }
            ],
        ),
        AIMessage(
            content="Attempt 2: rollback deployment",
            tool_calls=[
                {
                    "id": "call_attempt_2",
                    "name": "k8s_rollback_deployment",
                    "args": {
                        "namespace": "production",
                        "deployment_name": "checkout-web",
                    },
                }
            ],
        ),
    ]
    verify_results = [
        VerificationResult(
            verification_passed=False,
            observed_state_summary="Pods still CrashLoopBackOff",
            failure_feedback="Restart did not fix crash loop; rollback needed.",
        ),
        VerificationResult(
            verification_passed=True,
            observed_state_summary="2/2 pods Running and Ready after rollback.",
        ),
    ]
    plan_idx = 0
    verify_idx = 0

    class _LoopLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observations complete.", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            tool_names = {t.name for t in tools}

            class _Bound:
                async def ainvoke(self, messages: Any) -> AIMessage:
                    nonlocal plan_idx
                    if "k8s_restart_deployment" in tool_names:
                        msg = plan_attempts[min(plan_idx, len(plan_attempts) - 1)]
                        plan_idx += 1
                        return msg
                    return AIMessage(content="Observations complete.", tool_calls=[])

            return _Bound()

        def with_structured_output(self, schema: Any) -> Any:
            class _Struct:
                async def ainvoke(self, messages: Any) -> Any:
                    nonlocal verify_idx
                    if schema is RCAResult:
                        return rca
                    if schema is VerificationResult:
                        res = verify_results[min(verify_idx, len(verify_results) - 1)]
                        verify_idx += 1
                        return res
                    raise ValueError(schema)

            return _Struct()

    restart_tool = MagicMock()
    restart_tool.name = "k8s_restart_deployment"
    restart_tool.ainvoke = AsyncMock(return_value="Restarted checkout-web")

    rollback_tool = MagicMock()
    rollback_tool.name = "k8s_rollback_deployment"
    rollback_tool.ainvoke = AsyncMock(return_value="Rolled back checkout-web")

    tool_lookup = {
        "k8s_restart_deployment": restart_tool,
        "k8s_rollback_deployment": rollback_tool,
    }
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [restart_tool, rollback_tool] if mode == "mutate" else []
    )
    mock_registry.get_tool.side_effect = lambda name: tool_lookup.get(name)
    mock_registry.get_metadata.return_value = SimpleNamespace(
        blast_radius="service_wide", reversible=True
    )

    compiled = build_incident_graph(checkpointer=MemorySaver())
    llm = _LoopLLM()
    mock_publish = AsyncMock()
    mock_notify = AsyncMock()

    with (
        patch("nexus.engine.nodes.diagnose.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.plan.get_chat_model", return_value=llm),
        patch("nexus.engine.nodes.verify.get_chat_model", return_value=llm),
        patch(
            "nexus.engine.nodes.diagnose.get_tool_registry", return_value=mock_registry
        ),
        patch("nexus.engine.nodes.plan.get_tool_registry", return_value=mock_registry),
        patch(
            "nexus.engine.nodes.execute.get_tool_registry", return_value=mock_registry
        ),
        patch(
            "nexus.engine.nodes.verify.get_tool_registry", return_value=mock_registry
        ),
        patch(
            "nexus.engine.nodes.govern.evaluate_opa_policy",
            AsyncMock(
                return_value={
                    "allowed": True,
                    "requires_approval": True,
                    "reason": "OPA",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.workflow.send_approval_request_notification",
            mock_notify,
        ),
        patch(
            "nexus.services.notifications.notifier.send_approval_request_notification",
            mock_notify,
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            mock_publish,
        ),
    ):
        # 1. Initial run pauses at Attempt 1 approval
        paused_1 = await run_incident_workflow(
            compiled,
            {
                "incident_id": "inc-loop-01",
                "platform": "k8s",
                "max_remediation_attempts": 2,
                "raw_events": [{"message": "CrashLoopBackOff"}],
            },
        )
        assert paused_1["status"] == "AWAITING_APPROVAL"
        assert (
            paused_1["remediation_plan"]["steps"][0]["tool_name"]
            == "k8s_restart_deployment"
        )
        mock_publish.assert_not_called()

        # 2. Approve Attempt 1 -> execute restart -> verify fails -> loops to diagnose -> pauses at Attempt 2 approval!
        paused_2 = await handle_incident_approval(
            compiled, "inc-loop-01", approved=True
        )
        assert paused_2["status"] == "AWAITING_APPROVAL"
        assert paused_2["approved"] is None
        assert (
            paused_2["remediation_plan"]["steps"][0]["tool_name"]
            == "k8s_rollback_deployment"
        )
        mock_publish.assert_not_called()

        # 3. Approve Attempt 2 -> execute rollback -> verify passes -> learn -> RESOLVED + report published
        final_state = await handle_incident_approval(
            compiled, "inc-loop-01", approved=True
        )
        assert final_state["status"] == "RESOLVED"
        assert final_state["verification_passed"] is True
        executed = [r["tool_name"] for r in final_state["execution_results"]]
        assert executed == ["k8s_restart_deployment", "k8s_rollback_deployment"]
        mock_publish.assert_awaited_once()
