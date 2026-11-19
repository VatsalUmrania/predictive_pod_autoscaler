"""
Unit Tests for Schema-Bound Planning (`plan_node`), Direct Tool Execution (`execute_node`),
ReAct Post-Remediation Verification (`verify_node`), and K8s Observation Tools.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from nexus.engine.nodes.execution.execute import execute_node
from nexus.engine.nodes.execution.verify import verify_node
from nexus.engine.nodes.planning.plan import plan_node
from nexus.engine.reasoning.rca.result import VerificationResult
from nexus.engine.tools.k8s.observe.observe_tools import (
    fetch_pod_container_logs,
    inspect_deployment_status,
    locate_pod_and_owner,
)


@pytest.mark.asyncio
async def test_plan_node_binds_mutate_tools_and_attaches_registry_metadata():
    """plan_node uses llm.bind_tools(mutate_tools) and attaches blast_radius/reversible metadata."""
    fake_mutate_tool = MagicMock()
    fake_mutate_tool.name = "k8s_rollback_deployment"

    mock_registry = MagicMock()
    mock_registry.get_tools.return_value = [fake_mutate_tool]
    mock_registry.get_metadata.return_value = SimpleNamespace(
        blast_radius="service_wide", reversible=True
    )

    ai_response = AIMessage(
        content="Rolling back shop-demo to previous healthy revision.",
        tool_calls=[
            {
                "id": "call_rb_1",
                "name": "k8s_rollback_deployment",
                "args": {"namespace": "shop-ns", "deployment_name": "shop-demo"},
            }
        ],
    )
    bound_llm = MagicMock()
    bound_llm.ainvoke = AsyncMock(return_value=ai_response)
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = bound_llm

    state = {
        "incident_id": "inc-plan-01",
        "platform": "k8s",
        "namespace": "shop-ns",
        "resource_kind": "Deployment",
        "service_name": "shop-demo",
        "target_resource": "shop-ns/Deployment/shop-demo",
        "rca_result": {"failure_type": "readiness_probe_container_exit_zero"},
    }

    with (
        patch(
            "nexus.engine.nodes.planning.plan.get_tool_registry",
            return_value=mock_registry,
        ),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=mock_llm),
    ):
        res = await plan_node(state)

    mock_registry.get_tools.assert_called_once_with(mode="mutate", platform="k8s")
    mock_llm.bind_tools.assert_called_once_with([fake_mutate_tool])
    assert res["status"] == "PLANNED"
    steps = res["remediation_plan"]["steps"]
    assert len(steps) == 1
    assert steps[0]["tool_name"] == "k8s_rollback_deployment"
    assert steps[0]["tool_args"] == {
        "namespace": "shop-ns",
        "deployment_name": "shop-demo",
    }
    assert steps[0]["blast_radius"] == "service_wide"
    assert steps[0]["reversible"] is True


@pytest.mark.asyncio
async def test_plan_node_returns_unplannable_when_llm_emits_zero_tool_calls():
    fake_mutate_tool = MagicMock()
    fake_mutate_tool.name = "k8s_restart_deployment"
    mock_registry = MagicMock()
    mock_registry.get_tools.return_value = [fake_mutate_tool]

    bound_llm = MagicMock()
    bound_llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="Cannot safely remediate", tool_calls=[])
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = bound_llm

    with (
        patch(
            "nexus.engine.nodes.planning.plan.get_tool_registry",
            return_value=mock_registry,
        ),
        patch("nexus.engine.nodes.planning.plan.get_chat_model", return_value=mock_llm),
    ):
        res = await plan_node({"incident_id": "inc-unplan", "platform": "k8s"})

    assert res["status"] == "UNPLANNABLE"
    assert res["remediation_plan"]["steps"] == []


@pytest.mark.asyncio
async def test_execute_node_invokes_schema_validated_tool_args_directly():
    """execute_node calls await tool_fn.ainvoke(tool_args) without parameter guessing."""
    mock_tool = MagicMock()
    mock_tool.ainvoke = AsyncMock(return_value="deployment.apps/shop-demo rolled back")
    mock_registry = MagicMock()
    mock_registry.get_tool.return_value = mock_tool

    state = {
        "incident_id": "inc-exec-01",
        "remediation_attempts": 0,
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_rollback_deployment",
                    "tool_args": {
                        "namespace": "shop-ns",
                        "deployment_name": "shop-demo",
                    },
                    "reversible": True,
                }
            ]
        },
    }

    with patch(
        "nexus.engine.nodes.execution.execute.get_tool_registry",
        return_value=mock_registry,
    ):
        res = await execute_node(state)

    assert res["execution_status"] == "SUCCESS"
    assert res["status"] == "EXECUTED"
    assert res["remediation_attempts"] == 1
    assert res["rollback_available"] is True
    assert len(res["execution_results"]) == 1
    assert res["execution_results"][0]["status"] == "SUCCESS"
    mock_tool.ainvoke.assert_awaited_once_with(
        {"namespace": "shop-ns", "deployment_name": "shop-demo"}
    )


@pytest.mark.asyncio
async def test_execute_node_fails_when_zero_steps_or_tool_missing():
    res_empty = await execute_node(
        {"incident_id": "inc-empty", "remediation_plan": {"steps": []}}
    )
    assert res_empty["execution_status"] == "FAILED"
    assert res_empty["status"] == "EXECUTION_FAILED"

    mock_registry = MagicMock()
    mock_registry.get_tool.return_value = None
    with patch(
        "nexus.engine.nodes.execution.execute.get_tool_registry",
        return_value=mock_registry,
    ):
        res_missing = await execute_node(
            {
                "incident_id": "inc-missing",
                "remediation_plan": {
                    "steps": [
                        {"step": 1, "tool_name": "nonexistent_tool", "tool_args": {}}
                    ]
                },
            }
        )
    assert res_missing["execution_status"] == "FAILED"
    assert res_missing["remediation_attempts"] == 1


@pytest.mark.asyncio
async def test_verify_node_react_observation_loop_and_structured_result():
    """verify_node invokes observe tools in a ReAct loop and synthesizes VerificationResult."""
    observe_tool = MagicMock()
    observe_tool.name = "inspect_deployment_status"
    observe_tool.ainvoke = AsyncMock(
        return_value={
            "deployment_name": "shop-demo",
            "namespace": "shop-ns",
            "desired_replicas": 2,
            "ready_replicas": 2,
            "pods": [
                {"pod_name": "shop-demo-new-1", "phase": "Running", "ready": True}
            ],
        }
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
                        "id": "v_call_1",
                        "name": "inspect_deployment_status",
                        "args": {
                            "deployment_name": "shop-demo",
                            "namespace": "shop-ns",
                        },
                    }
                ],
            ),
            AIMessage(content="All replicas ready.", tool_calls=[]),
        ]
    )
    structured_llm = MagicMock()
    structured_llm.ainvoke = AsyncMock(
        return_value=VerificationResult(
            verification_passed=True,
            observed_state_summary="2/2 replicas ready and passing readiness probes.",
        )
    )
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = bound_llm
    mock_llm.with_structured_output.return_value = structured_llm

    with (
        patch(
            "nexus.engine.nodes.execution.verify.get_tool_registry",
            return_value=mock_registry,
        ),
        patch(
            "nexus.engine.nodes.execution.verify.get_chat_model", return_value=mock_llm
        ),
    ):
        res = await verify_node(
            {
                "incident_id": "inc-ver-01",
                "platform": "k8s",
                "namespace": "shop-ns",
                "resource_kind": "Deployment",
                "service_name": "shop-demo",
                "execution_results": [
                    {
                        "step": 1,
                        "tool_name": "k8s_rollback_deployment",
                        "status": "SUCCESS",
                    }
                ],
            }
        )

    observe_tool.ainvoke.assert_awaited_once_with(
        {"deployment_name": "shop-demo", "namespace": "shop-ns"}
    )
    assert res["verification_passed"] is True
    assert res["status"] == "RESOLVED"


def test_k8s_observe_tools_locate_logs_and_deployment_status():
    """Verify locate_pod_and_owner, fetch_pod_container_logs, and inspect_deployment_status."""
    pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name="shop-demo-59bd7ddcf9-t2skj",
            namespace="shop-ns",
            labels={"app.kubernetes.io/name": "shop-demo", "nexus.sre/enabled": "true"},
            owner_references=[
                SimpleNamespace(kind="ReplicaSet", name="shop-demo-59bd7ddcf9")
            ],
        ),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name="shop-backend",
                    ready=False,
                    restart_count=4,
                    state=SimpleNamespace(
                        running=None,
                        waiting=SimpleNamespace(
                            reason="CrashLoopBackOff", message="back-off"
                        ),
                        terminated=None,
                    ),
                    last_state=SimpleNamespace(
                        terminated=SimpleNamespace(
                            exit_code=0,
                            reason="Completed",
                            message="",
                            finished_at="2026-11-18T12:00:00Z",
                        )
                    ),
                )
            ],
        ),
        spec=SimpleNamespace(
            containers=[
                SimpleNamespace(
                    name="shop-backend",
                    image="shop-demo:latest",
                    command=["sh", "-c", "exit 0"],
                    args=None,
                    ports=[SimpleNamespace(container_port=8001)],
                    resources=SimpleNamespace(
                        to_dict=lambda: {"limits": {"memory": "256Mi"}}
                    ),
                    readiness_probe=SimpleNamespace(
                        to_dict=lambda: {"httpGet": {"port": 8001, "path": "/health"}}
                    ),
                    liveness_probe=None,
                )
            ]
        ),
    )
    rs = SimpleNamespace(
        metadata=SimpleNamespace(
            owner_references=[SimpleNamespace(kind="Deployment", name="shop-demo")]
        )
    )
    dep = SimpleNamespace(
        metadata=SimpleNamespace(
            name="shop-demo",
            namespace="shop-ns",
            labels={"nexus.sre/enabled": "true"},
        ),
        spec=SimpleNamespace(
            replicas=2,
            selector=SimpleNamespace(
                match_labels={"app.kubernetes.io/name": "shop-demo"}
            ),
        ),
        status=SimpleNamespace(
            ready_replicas=2,
            available_replicas=2,
            unavailable_replicas=0,
            conditions=[
                SimpleNamespace(
                    type="Available",
                    status="True",
                    reason="MinimumReplicasAvailable",
                    message="ok",
                )
            ],
        ),
    )

    core_v1 = MagicMock()
    core_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=[pod])
    core_v1.read_namespaced_pod_log.side_effect = [
        "current log line",
        "previous crash line",
    ]
    core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])

    apps_v1 = MagicMock()
    apps_v1.read_namespaced_replica_set.return_value = rs
    apps_v1.read_namespaced_deployment.return_value = dep

    with (
        patch(
            "nexus.engine.tools.k8s.observe.observe_tools.get_k8s_core_api",
            return_value=core_v1,
        ),
        patch(
            "nexus.engine.tools.k8s.observe.observe_tools.get_k8s_apps_api",
            return_value=apps_v1,
        ),
    ):
        loc = locate_pod_and_owner.invoke({"pod_name": "shop-demo-59bd7ddcf9-t2skj"})
        assert loc["namespace"] == "shop-ns"
        assert loc["owner_kind"] == "Deployment"
        assert loc["owner_name"] == "shop-demo"
        assert (
            loc["container_statuses"][0]["last_terminated_state"]["terminated"][
                "exit_code"
            ]
            == 0
        )

        logs = fetch_pod_container_logs.invoke(
            {"pod_name": "shop-demo-59bd7ddcf9-t2skj", "namespace": "shop-ns"}
        )
        assert logs["current_container_logs"] == "current log line"
        assert logs["previous_terminated_container_logs"] == "previous crash line"

        dep_status = inspect_deployment_status.invoke(
            {"deployment_name": "shop-demo", "namespace": "shop-ns"}
        )
        assert dep_status["nexus_registered"] is True
        assert dep_status["desired_replicas"] == 2
        assert dep_status["ready_replicas"] == 2
