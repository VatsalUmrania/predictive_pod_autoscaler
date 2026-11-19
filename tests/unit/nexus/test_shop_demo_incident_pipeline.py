"""
End-to-End Integration Test: shop-demo Readiness Probe Failure (Exit Code 0)
=============================================================================
Walks through the complete zero-hardcoding agentic pipeline:
  1. Pure OTLP extraction (missing namespace/service_name stay None)
  2. ReAct diagnose_node discovers shop-ns/Deployment/shop-demo & open failure_type
  3. Schema-bound plan_node selects mutation tool call
  4. Attribute-based govern_node triggers LangGraph interrupt() at approval_node
     (NO premature Post-Incident Report, discovered target synced to DB)
  5. Operator approves -> Command(resume=...) -> execute_node -> ReAct verify_node
     -> learn_node -> Post-Incident Report published.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from nexus.engine.reasoning.rca.result import (
    DiscoveredTarget,
    RCAResult,
    VerificationResult,
)
from nexus.engine.workflow.builder import build_incident_graph
from nexus.engine.workflow.workflow import run_incident_workflow
from nexus.engine.workflow.workflow_approval import handle_incident_approval
from nexus.services.api.ingest import extract_telemetry_context


@pytest.mark.asyncio
async def test_shop_demo_readiness_probe_exit_zero_end_to_end():
    # 1. Stage 2 OTel Push -> Pure OTLP extraction (missing namespace/controller stay None)
    raw_k8s_event_body = {
        "object": {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": "Readiness probe failed: Get http://10.244.0.15:8001/health: dial tcp 10.244.0.15:8001: connect: connection refused",
            "involvedObject": {
                "kind": "Pod",
                "name": "shop-demo-59bd7ddcf9-t2skj",
            },
        }
    }
    telemetry_ctx = extract_telemetry_context(
        resource_attrs={}, log_attrs={}, body=raw_k8s_event_body
    )
    assert telemetry_ctx["platform"] == "k8s"
    assert telemetry_ctx["namespace"] is None
    assert telemetry_ctx["service_name"] is None
    assert telemetry_ctx["involved_object_name"] == "shop-demo-59bd7ddcf9-t2skj"

    # 2. Stage 3 On-Demand Pull Tools & Mutation Tool
    locate_tool = MagicMock()
    locate_tool.name = "locate_pod_and_owner"
    locate_tool.ainvoke = AsyncMock(
        return_value={
            "pod_name": "shop-demo-59bd7ddcf9-t2skj",
            "namespace": "shop-ns",
            "owner_kind": "Deployment",
            "owner_name": "shop-demo",
            "container_statuses": [
                {
                    "container_name": "shop-backend",
                    "ready": False,
                    "restart_count": 3,
                    "last_terminated_state": {
                        "terminated": {"exit_code": 0, "reason": "Completed"}
                    },
                }
            ],
            "container_specs": [
                {
                    "container_name": "shop-backend",
                    "command": ["sh", "-c", "exit 0"],
                    "readiness_probe": {"httpGet": {"path": "/health", "port": 8001}},
                }
            ],
        }
    )

    logs_tool = MagicMock()
    logs_tool.name = "fetch_pod_container_logs"
    logs_tool.ainvoke = AsyncMock(
        return_value={
            "pod_name": "shop-demo-59bd7ddcf9-t2skj",
            "namespace": "shop-ns",
            "current_container_logs": "",
            "previous_terminated_container_logs": "Exiting immediately with status 0",
        }
    )

    inspect_dep_tool = MagicMock()
    inspect_dep_tool.name = "inspect_deployment_status"
    inspect_dep_tool.ainvoke = AsyncMock(
        return_value={
            "deployment_name": "shop-demo",
            "namespace": "shop-ns",
            "nexus_registered": True,
            "desired_replicas": 2,
            "ready_replicas": 2,
            "available_replicas": 2,
            "unavailable_replicas": 0,
            "pods": [
                {
                    "pod_name": "shop-demo-healthy-1",
                    "phase": "Running",
                    "ready": True,
                    "restarts": 0,
                },
                {
                    "pod_name": "shop-demo-healthy-2",
                    "phase": "Running",
                    "ready": True,
                    "restarts": 0,
                },
            ],
        }
    )

    mutate_tool = MagicMock()
    mutate_tool.name = "k8s_remove_container_command"
    mutate_tool.ainvoke = AsyncMock(
        return_value="Removed container command for shop-demo."
    )

    observe_tools = [locate_tool, logs_tool, inspect_dep_tool]
    mutate_tools = [mutate_tool]
    all_tools = {t.name: t for t in [*observe_tools, *mutate_tools]}

    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        mutate_tools if mode == "mutate" else observe_tools
    )
    mock_registry.get_tool.side_effect = lambda name: all_tools.get(name)
    mock_registry.get_metadata.return_value = SimpleNamespace(
        blast_radius="service_wide", reversible=True
    )

    # 3. Deterministic LLM driving ReAct Diagnose -> Schema-Bound Plan -> ReAct Verify
    diag_step = 0
    verify_step = 0

    class _ShopDemoLLM:
        def bind_tools(self, tools: list[Any]) -> Any:
            names = {t.name for t in tools}

            class _Bound:
                async def ainvoke(self, messages: Any) -> AIMessage:
                    nonlocal diag_step, verify_step
                    if "k8s_remove_container_command" in names:
                        return AIMessage(
                            content="Removing erroneous command override on shop-ns/Deployment/shop-demo.",
                            tool_calls=[
                                {
                                    "id": "call_fix_cmd",
                                    "name": "k8s_remove_container_command",
                                    "args": {
                                        "namespace": "shop-ns",
                                        "deployment_name": "shop-demo",
                                    },
                                }
                            ],
                        )
                    # Distinguish diagnose vs verify via system prompt text
                    sys_text = messages[0].content
                    if "Investigator" in sys_text:
                        if diag_step == 0:
                            diag_step += 1
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "call_loc",
                                        "name": "locate_pod_and_owner",
                                        "args": {
                                            "pod_name": "shop-demo-59bd7ddcf9-t2skj"
                                        },
                                    }
                                ],
                            )
                        if diag_step == 1:
                            diag_step += 1
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "call_logs",
                                        "name": "fetch_pod_container_logs",
                                        "args": {
                                            "pod_name": "shop-demo-59bd7ddcf9-t2skj",
                                            "namespace": "shop-ns",
                                        },
                                    }
                                ],
                            )
                        return AIMessage(content="Root cause confirmed.", tool_calls=[])
                    else:
                        if verify_step == 0:
                            verify_step += 1
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "id": "call_verify_dep",
                                        "name": "inspect_deployment_status",
                                        "args": {
                                            "deployment_name": "shop-demo",
                                            "namespace": "shop-ns",
                                        },
                                    }
                                ],
                            )
                        return AIMessage(content="Deployment healthy.", tool_calls=[])

            return _Bound()

        def with_structured_output(self, schema: Any) -> Any:
            class _Struct:
                async def ainvoke(self, messages: Any) -> Any:
                    if schema is RCAResult:
                        return RCAResult(
                            target=DiscoveredTarget(
                                platform="k8s",
                                namespace_or_region="shop-ns",
                                resource_kind="Deployment",
                                resource_name="shop-demo",
                            ),
                            failure_type="readiness_probe_container_exit_zero",
                            root_cause_summary="Container spec has command ['sh', '-c', 'exit 0'] causing exitCode 0 before port 8001 starts.",
                            confidence=0.96,
                            evidence=["exit_code=0", "command=['sh', '-c', 'exit 0']"],
                            can_auto_remediate=True,
                            recommended_strategy="Remove container command override on shop-ns/Deployment/shop-demo.",
                        )
                    if schema is VerificationResult:
                        return VerificationResult(
                            verification_passed=True,
                            observed_state_summary="shop-ns/Deployment/shop-demo has 2/2 ready replicas and all pods Ready.",
                        )
                    raise ValueError(schema)

            return _Struct()

    compiled = build_incident_graph(checkpointer=MemorySaver())
    llm = _ShopDemoLLM()
    mock_notify = AsyncMock()
    mock_publish = AsyncMock()
    mock_db = MagicMock()
    mock_db.update_discovered_target = AsyncMock()

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
                    "reason": "OPA approval required",
                }
            ),
        ),
        patch(
            "nexus.engine.workflow.workflow.send_approval_request_notification",
            mock_notify,
        ),
        patch(
            "nexus.services.reporting.incident_report.publish_post_incident_report",
            mock_publish,
        ),
        patch("nexus.services.api.status_api.context.db_client", mock_db),
    ):
        # Phase A: Ingest -> Triage -> ReAct Diagnose -> Schema-Bound Plan -> Govern -> Approval Interrupt
        paused = await run_incident_workflow(
            compiled,
            {
                "incident_id": "inc-shop-demo-e2e",
                "platform": telemetry_ctx["platform"],
                "namespace": telemetry_ctx["namespace"],
                "service_name": telemetry_ctx["service_name"],
                "raw_events": [telemetry_ctx],
            },
        )

        assert paused["status"] == "AWAITING_APPROVAL"
        assert paused["namespace"] == "shop-ns"
        assert paused["resource_kind"] == "Deployment"
        assert paused["service_name"] == "shop-demo"
        assert paused["target_resource"] == "shop-ns/Deployment/shop-demo"
        assert paused["failure_class"] == "readiness_probe_container_exit_zero"
        assert (
            paused["remediation_plan"]["steps"][0]["tool_name"]
            == "k8s_remove_container_command"
        )
        assert paused["remediation_plan"]["steps"][0]["tool_args"] == {
            "namespace": "shop-ns",
            "deployment_name": "shop-demo",
        }
        # DB target identity synced before Slack approval prompt; zero premature Post-Incident Report
        mock_db.update_discovered_target.assert_awaited()
        mock_notify.assert_awaited_once()
        mock_publish.assert_not_called()

        # Phase B: Operator Approves -> Command(resume=...) -> Execute -> ReAct Verify -> Learn -> Report
        final = await handle_incident_approval(
            compiled,
            incident_id="inc-shop-demo-e2e",
            approved=True,
            approver="vatsal",
            reason="Verified bad command override",
        )

        assert final["status"] == "RESOLVED"
        assert final["execution_status"] == "SUCCESS"
        assert len(final["execution_results"]) == 1
        assert (
            final["execution_results"][0]["tool_name"] == "k8s_remove_container_command"
        )
        assert final["verification_passed"] is True
        locate_tool.ainvoke.assert_awaited_once()
        logs_tool.ainvoke.assert_awaited_once()
        mutate_tool.ainvoke.assert_awaited_once_with(
            {"namespace": "shop-ns", "deployment_name": "shop-demo"}
        )
        inspect_dep_tool.ainvoke.assert_awaited_once_with(
            {"deployment_name": "shop-demo", "namespace": "shop-ns"}
        )
        mock_publish.assert_awaited_once_with("inc-shop-demo-e2e", final)
