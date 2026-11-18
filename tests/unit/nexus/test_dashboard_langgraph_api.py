"""
Unit & Integration Tests for the Agentic LangGraph Developer Dashboard API:
  - determine_active_graph_node & serialize_incident_for_dashboard
  - Live LangGraph checkpointer merging in GET /developer/incidents & /api/v1/status/incidents
  - Inline HITL POST /developer/incidents/{id}/approve & /reject -> Command(resume=...)
  - Upgraded developer-dashboard.html assertions
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from nexus.graph.checkpointer import GraphCheckpointStore
from nexus.graph.rca.result import DiscoveredTarget, RCAResult, VerificationResult
from nexus.graph.workflow import IncidentWorkflow
from nexus.integration.dashboard_incidents import (
    _in_memory_incidents,
    determine_active_graph_node,
    serialize_incident_for_dashboard,
)
from nexus.observability.status_api import app, context


def _make_fake_llm(
    rca: RCAResult,
    plan_tool_calls: list[dict[str, Any]],
    verify: VerificationResult,
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
            if any(tc["name"] in self._tool_names for tc in plan_tool_calls):
                return AIMessage(
                    content="Applying minimal remediation patch",
                    tool_calls=plan_tool_calls,
                )
            return AIMessage(content="Cluster observation complete", tool_calls=[])

    class _FakeLLM:
        async def ainvoke(self, messages: Any) -> AIMessage:
            return AIMessage(content="Observation complete", tool_calls=[])

        def bind_tools(self, tools: list[Any]) -> Any:
            return _FakeBound(tools)

        def with_structured_output(self, schema: Any) -> Any:
            return _FakeStructured(schema)

    return _FakeLLM()


def test_determine_active_graph_node_mappings():
    assert determine_active_graph_node({"status": "DIAGNOSED"}, ["approval"]) == "APPROVAL"
    assert determine_active_graph_node({"triage_status": "IGNORED"}) == "TRIAGE"
    assert determine_active_graph_node({"status": "AWAITING_APPROVAL"}) == "APPROVAL"
    assert determine_active_graph_node({"approval_status": "PENDING"}) == "APPROVAL"
    assert determine_active_graph_node({"status": "DIAGNOSED"}) == "DIAGNOSE"
    assert determine_active_graph_node({"status": "PLANNED"}) == "PLAN"
    assert determine_active_graph_node({"status": "UNPLANNABLE"}) == "PLAN"
    assert determine_active_graph_node({"status": "APPROVED"}) == "GOVERN"
    assert determine_active_graph_node({"status": "AUTO_APPROVED"}) == "GOVERN"
    assert determine_active_graph_node({"status": "POLICY_DENIED"}) == "GOVERN"
    assert determine_active_graph_node({"status": "EXECUTED"}) == "EXECUTE"
    assert determine_active_graph_node({"status": "EXECUTION_FAILED"}) == "EXECUTE"
    assert determine_active_graph_node({"status": "VERIFICATION_FAILED"}) == "VERIFY"
    assert determine_active_graph_node({"status": "ROLLED_BACK"}) == "ROLLBACK"
    assert determine_active_graph_node({"status": "RESOLVED"}) == "LEARN"
    assert determine_active_graph_node({"status": "ESCALATED"}) == "ESCALATE"
    # Legacy outcome fallbacks
    assert determine_active_graph_node({"outcome": "success"}) == "LEARN"
    assert determine_active_graph_node({"outcome": "rolled_back"}) == "ROLLBACK"
    assert determine_active_graph_node({"outcome": "failed"}) == "ESCALATE"


def test_serialize_incident_for_dashboard_enriches_agentic_and_preserves_legacy():
    state = {
        "platform": "k8s",
        "namespace": "default",
        "resource_kind": "Deployment",
        "service_name": "shop-demo",
        "target_resource": "default/Deployment/shop-demo",
        "raw_events": [
            {
                "involved_object_kind": "Pod",
                "involved_object_name": "shop-demo-59bd7ddcf9-t2skj",
            }
        ],
        "severity": "critical",
        "triage_status": "TRIAGED",
        "failure_class": "readiness_probe_exit_zero",
        "suspected_cause": "Readiness probe command exits 1 on /healthz.",
        "confidence": 0.92,
        "min_confidence_threshold": 0.65,
        "rca_result": {
            "target": {
                "platform": "k8s",
                "namespace_or_region": "default",
                "resource_kind": "Deployment",
                "resource_name": "shop-demo",
            },
            "failure_type": "readiness_probe_exit_zero",
            "root_cause_summary": "Readiness probe command exits 1 on /healthz.",
            "confidence": 0.92,
            "evidence": ["Pod condition Ready=False", "Probe command failed"],
            "can_auto_remediate": True,
            "recommended_strategy": "Rollback Deployment/shop-demo to prior revision.",
        },
        "remediation_plan": {
            "rationale": "Revert bad probe deployment",
            "steps": [
                {
                    "step": 1,
                    "tool_call_id": "call_1",
                    "tool_name": "k8s_rollback_deployment",
                    "tool_args": {"namespace": "default", "deployment_name": "shop-demo"},
                    "blast_radius": "service_wide",
                    "reversible": True,
                }
            ],
        },
        "policy_decision": {
            "allowed": True,
            "requires_approval": True,
            "reason": "Service-wide mutation requires operator approval",
        },
        "requires_approval": True,
        "approval_status": "PENDING",
        "execution_results": [],
        "remediation_attempts": 0,
        "max_remediation_attempts": 2,
        "status": "AWAITING_APPROVAL",
    }

    serialized = serialize_incident_for_dashboard(
        incident_id="INC-REACT-01",
        state=state,
        next_nodes=["approval"],
        updated_at="2026-11-18T14:00:00Z",
        legacy_record={"accepted_by": "oncall-dev", "level": 3},
    )

    assert serialized["incident_id"] == "INC-REACT-01"
    assert serialized["platform"] == "k8s"
    assert serialized["involved_object"] == "Pod/shop-demo-59bd7ddcf9-t2skj"
    assert serialized["target_resource"] == "default/Deployment/shop-demo"
    assert serialized["status"] == "AWAITING_APPROVAL"
    assert serialized["active_node"] == "APPROVAL"
    assert serialized["next_nodes"] == ["approval"]
    assert serialized["failure_class"] == "readiness_probe_exit_zero"
    assert serialized["confidence"] == 0.92
    assert serialized["can_auto_remediate"] is True
    assert len(serialized["evidence"]) == 2
    assert serialized["remediation_plan"]["steps"][0]["tool_name"] == "k8s_rollback_deployment"
    assert serialized["policy_decision"]["requires_approval"] is True
    assert serialized["approval"]["is_interrupted"] is True
    assert serialized["post_incident_report_ready"] is False
    # Legacy preservation
    assert serialized["accepted_by"] == "oncall-dev"
    assert serialized["level"] == 3


@pytest.mark.asyncio
async def test_dashboard_merges_live_checkpoint_and_inline_approve_resumes_graph(
    monkeypatch,
):
    """
    Full dashboard HITL flow:
      1. IncidentWorkflow pauses at approval_node via interrupt().
      2. GET /developer/incidents and GET /api/v1/status/incidents both surface the
         paused thread with full LangGraph checkpoint fields.
      3. POST /developer/incidents/{id}/approve resumes via Command(resume=...) with
         approver and operator reason.
      4. Subsequent GET /developer/incidents shows RESOLVED, LEARN node, execution
         results, verification_passed=True, and post_incident_report_ready=True.
    """
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="shop-demo",
        ),
        failure_type="readiness_probe_exit_zero",
        root_cause_summary="Readiness probe failed on shop-demo.",
        confidence=0.94,
        evidence=["kubectl describe pod shop-demo-abc123: Readiness probe failed"],
        can_auto_remediate=True,
        recommended_strategy="Rollback Deployment/shop-demo.",
    )
    plan_calls = [
        {
            "id": "call_dash_1",
            "name": "k8s_rollback_deployment",
            "args": {"namespace": "production", "deployment_name": "shop-demo"},
        }
    ]
    verif = VerificationResult(
        verification_passed=True,
        observed_state_summary="Deployment shop-demo readyReplicas=2/2.",
    )
    fake_llm = _make_fake_llm(rca, plan_calls, verif)

    mock_tool = MagicMock()
    mock_tool.ainvoke = AsyncMock(return_value="Rolled back Deployment/shop-demo")
    real_tool_stub = MagicMock()
    real_tool_stub.name = "k8s_rollback_deployment"
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [real_tool_stub] if mode == "mutate" else []
    )
    mock_registry.get_tool.return_value = mock_tool
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="service_wide", reversible=True
    )

    workflow = IncidentWorkflow(checkpointer_store=GraphCheckpointStore(MemorySaver()))

    async def _failing_db():
        raise RuntimeError("offline test")

    monkeypatch.setattr("nexus.integration.dashboard.get_database_client", _failing_db)

    prev_wf = context.workflow
    prev_orc = context.orchestrator
    prev_db = context.db_client
    prev_mem = list(_in_memory_incidents)
    _in_memory_incidents.clear()
    context.workflow = workflow
    context.orchestrator = None
    context.db_client = None

    try:
        with (
            patch("nexus.graph.nodes.diagnose.get_chat_model", return_value=fake_llm),
            patch("nexus.graph.nodes.plan.get_chat_model", return_value=fake_llm),
            patch("nexus.graph.nodes.verify.get_chat_model", return_value=fake_llm),
            patch("nexus.graph.nodes.diagnose.get_tool_registry", return_value=mock_registry),
            patch("nexus.graph.nodes.plan.get_tool_registry", return_value=mock_registry),
            patch("nexus.graph.nodes.execute.get_tool_registry", return_value=mock_registry),
            patch("nexus.graph.nodes.verify.get_tool_registry", return_value=mock_registry),
            patch(
                "nexus.graph.nodes.govern.evaluate_opa_policy",
                AsyncMock(
                    return_value={
                        "allowed": True,
                        "requires_approval": True,
                        "reason": "Production rollout requires human sign-off",
                    }
                ),
            ),
            patch("nexus.graph.workflow.send_approval_request_notification", AsyncMock()),
            patch("nexus.graph.workflow.sync_discovered_target_identity", AsyncMock()),
            patch("nexus.graph.nodes.diagnose.sync_discovered_target_identity", AsyncMock()),
            patch("nexus.graph.nodes.approval.sync_discovered_target_identity", AsyncMock()),
            patch(
                "nexus.integration.incident_report.publish_post_incident_report",
                AsyncMock(),
            ) as mock_publish,
        ):
            await workflow.run_incident(
                {
                    "incident_id": "INC-HITL-DASH-01",
                    "platform": "k8s",
                    "raw_events": [
                        {
                            "involved_object_kind": "Pod",
                            "involved_object_name": "shop-demo-abc123",
                            "message": "Readiness probe failed",
                        }
                    ],
                },
                thread_id="INC-HITL-DASH-01",
            )

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # 1. Paused thread appears on both /developer/incidents and /api/v1/status/incidents
                for url in ("/developer/incidents", "/api/v1/status/incidents"):
                    res = await client.get(url)
                    assert res.status_code == 200
                    items = res.json()
                    inc = next(
                        (i for i in items if i["incident_id"] == "INC-HITL-DASH-01"), None
                    )
                    assert inc is not None, f"Missing paused incident on {url}"
                    assert inc["status"] == "AWAITING_APPROVAL"
                    assert inc["active_node"] == "APPROVAL"
                    assert inc["approval"]["is_interrupted"] is True
                    assert inc["involved_object"] == "Pod/shop-demo-abc123"
                    assert inc["target_resource"] == "production/Deployment/shop-demo"
                    assert inc["failure_class"] == "readiness_probe_exit_zero"
                    assert inc["policy_decision"]["reason"] == "Production rollout requires human sign-off"
                    assert inc["remediation_plan"]["steps"][0]["tool_name"] == "k8s_rollback_deployment"
                    assert inc["post_incident_report_ready"] is False

                # 2. Operator approves inline from dashboard
                res_approve = await client.post(
                    "/developer/incidents/INC-HITL-DASH-01/approve",
                    json={
                        "incident_id": "INC-HITL-DASH-01",
                        "approved": True,
                        "approver": "sre-vatsal",
                        "reason": "Verified rollout window",
                    },
                )
                assert res_approve.status_code == 200
                body = res_approve.json()
                assert body["status"] == "approved"
                assert body["approved_by"] == "sre-vatsal"
                assert body["outcome"]["status"] == "RESOLVED"
                mock_tool.ainvoke.assert_awaited_once_with(plan_calls[0]["args"])
                mock_publish.assert_awaited_once()

                # 3. Feed now reflects RESOLVED + execution + verification + report ready
                res_after = await client.get("/developer/incidents")
                assert res_after.status_code == 200
                inc_after = next(
                    (
                        i
                        for i in res_after.json()
                        if i["incident_id"] == "INC-HITL-DASH-01"
                    ),
                    None,
                )
                assert inc_after is not None
                assert inc_after["status"] == "RESOLVED"
                assert inc_after["active_node"] == "LEARN"
                assert inc_after["approval"]["is_interrupted"] is False
                assert inc_after["approval"]["approved_by"] == "sre-vatsal"
                assert inc_after["approval"]["approval_reason"] == "Verified rollout window"
                assert len(inc_after["execution"]["results"]) == 1
                assert inc_after["verification"]["verification_passed"] is True
                assert inc_after["post_incident_report_ready"] is True
    finally:
        context.workflow = prev_wf
        context.orchestrator = prev_orc
        context.db_client = prev_db
        _in_memory_incidents.clear()
        _in_memory_incidents.extend(prev_mem)


@pytest.mark.asyncio
async def test_dashboard_inline_reject_escalates_and_keeps_report_guarded(monkeypatch):
    """Rejecting from the dashboard escalates with 0 executed steps and post_incident_report_ready=False."""
    rca = RCAResult(
        target=DiscoveredTarget(
            platform="k8s",
            namespace_or_region="production",
            resource_kind="Deployment",
            resource_name="billing-api",
        ),
        failure_type="oom_killed",
        root_cause_summary="OOMKilled on billing-api.",
        confidence=0.91,
        evidence=["Exit code 137"],
        can_auto_remediate=True,
        recommended_strategy="Restart billing-api.",
    )
    plan_calls = [
        {
            "id": "call_rej_1",
            "name": "k8s_restart_deployment",
            "args": {"namespace": "production", "deployment_name": "billing-api"},
        }
    ]
    verif = VerificationResult(verification_passed=True, observed_state_summary="OK")
    fake_llm = _make_fake_llm(rca, plan_calls, verif)

    real_tool_stub = MagicMock()
    real_tool_stub.name = "k8s_restart_deployment"
    mock_registry = MagicMock()
    mock_registry.get_tools.side_effect = lambda mode, platform=None: (
        [real_tool_stub] if mode == "mutate" else []
    )
    mock_registry.get_metadata.return_value = MagicMock(
        blast_radius="pod", reversible=True
    )

    workflow = IncidentWorkflow(checkpointer_store=GraphCheckpointStore(MemorySaver()))

    async def _failing_db():
        raise RuntimeError("offline test")

    monkeypatch.setattr("nexus.integration.dashboard.get_database_client", _failing_db)

    prev_wf = context.workflow
    prev_orc = context.orchestrator
    prev_db = context.db_client
    prev_mem = list(_in_memory_incidents)
    _in_memory_incidents.clear()
    context.workflow = workflow
    context.orchestrator = None
    context.db_client = None

    try:
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
                        "reason": "OPA approval required",
                    }
                ),
            ),
            patch("nexus.graph.workflow.send_approval_request_notification", AsyncMock()),
            patch("nexus.graph.workflow.sync_discovered_target_identity", AsyncMock()),
            patch("nexus.graph.nodes.diagnose.sync_discovered_target_identity", AsyncMock()),
            patch("nexus.graph.nodes.approval.sync_discovered_target_identity", AsyncMock()),
            patch(
                "nexus.integration.incident_report.publish_post_incident_report",
                AsyncMock(),
            ) as mock_publish,
        ):
            await workflow.run_incident(
                {
                    "incident_id": "INC-HITL-REJ-01",
                    "platform": "k8s",
                    "raw_events": [{"message": "OOMKilled"}],
                },
                thread_id="INC-HITL-REJ-01",
            )

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res_reject = await client.post(
                    "/developer/incidents/INC-HITL-REJ-01/reject",
                    json={
                        "incident_id": "INC-HITL-REJ-01",
                        "approved": False,
                        "approver": "sre-bob",
                        "reason": "Change freeze active",
                    },
                )
                assert res_reject.status_code == 200
                body = res_reject.json()
                assert body["status"] == "rejected"
                assert body["outcome"]["status"] == "ESCALATED"
                assert body["outcome"].get("execution_results", []) == []
                mock_publish.assert_not_called()
    finally:
        context.workflow = prev_wf
        context.orchestrator = prev_orc
        context.db_client = prev_db
        _in_memory_incidents.clear()
        _in_memory_incidents.extend(prev_mem)


@pytest.mark.asyncio
async def test_dashboard_html_upgraded_security_accessibility_and_routes():
    """Verify upgraded developer-dashboard.html routes, dark color-scheme, disclosures, KPIs, and no inline onclick."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        html = res.text

        assert '<meta name="color-scheme" content="dark light" />' in html
        assert "color-scheme: dark;" in html
        assert "ph-shield-check" in html
        assert "/developer/incidents/${encodeURIComponent(inc.incident_id)}/report" in html
        assert "/api/v1/incidents/${encodeURIComponent(inc.incident_id)}/report" not in html
        assert 'id="kpi-success-rate"' in html
        assert 'id="kpi-ppa-events"' in html
        assert "evidence-details" in html
        assert "Deterministic Rollback Results" in html
        assert "onclick=" not in html
