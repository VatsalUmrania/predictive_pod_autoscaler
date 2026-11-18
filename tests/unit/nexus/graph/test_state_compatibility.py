"""
Unit tests for NEXUS IncidentState schema, AnomalySignal serialization,
initial state building (without fabricated defaults), and triage node.
"""

from __future__ import annotations

import pytest
from langgraph.graph import StateGraph

from nexus.bus.events.event import AnomalySignal
from nexus.graph.builder import build_incident_graph
from nexus.graph.nodes.triage import triage_node
from nexus.graph.state import (
    IncidentGraphState,
    IncidentState,
    PlannedToolStep,
    PlanStep,
    VerificationCondition,
)
from nexus.graph.workflow_events import build_initial_state


def test_incident_graph_state_compilation():
    """Verify StateGraph compiles cleanly with IncidentState."""
    graph = build_incident_graph()
    assert graph is not None
    assert IncidentGraphState is IncidentState


def test_build_initial_state_preserves_none_identity_and_defaults():
    """Verify build_initial_state preserves None for unknown identity and initializes loop fields."""
    data = {
        "raw_events": [
            {
                "agent": "k8s",
                "severity": "warning",
                "involved_object_name": "checkout-abc",
            }
        ],
    }
    state = build_initial_state(data, inc_id="inc-test-01")

    assert state["incident_id"] == "inc-test-01"
    assert state["status"] == "DETECTED"
    assert len(state["raw_events"]) == 1
    assert state["platform"] == "k8s"
    assert state["namespace"] is None
    assert state["service_name"] is None
    assert state["execution_results"] == []
    assert state["remediation_attempts"] == 0
    assert state["max_remediation_attempts"] == 2
    assert state["rollback_available"] is False
    assert state["rollback_results"] == []


def test_anomaly_signal_model_and_nats_serialization():
    """Verify AnomalySignal model fields, canonical NATS subject, and JSON serialization."""
    signal = AnomalySignal(
        source="cloudwatch",
        provider="aws",
        environment="production",
        target_service="checkout-service",
        target_resource_id="arn:aws:dynamodb:us-east-1:123456789012:table/orders",
        symptom_summary="DynamoDB write throttle rate exceeding 15%",
        raw_metrics_or_alert={"wcu": 50, "throttle_events": 120},
    )

    assert signal.provider == "aws"
    assert signal.target_service == "checkout-service"
    assert signal.nats_subject() == "nexus.anomaly.aws.checkout-service"

    payload = signal.to_nats_payload()
    deserialized = AnomalySignal.from_nats_payload(payload)
    assert deserialized.signal_id == signal.signal_id
    assert deserialized.target_service == "checkout-service"
    assert deserialized.raw_metrics_or_alert["wcu"] == 50


@pytest.mark.asyncio
async def test_triage_node_extracts_explicit_hints_without_fabricating_defaults():
    """Verify triage_node extracts explicit hints and leaves missing fields as None."""
    state: IncidentState = {
        "incident_id": "inc-triage-01",
        "raw_events": [
            {
                "platform": "kubernetes",
                "involved_object_name": "payment-api-5d8f-xyz",
                "namespace": "production",
                "severity": "critical",
                "signal_type": "pod_oomkilled",
            }
        ],
    }

    result = await triage_node(state)
    assert result["platform"] == "k8s"
    assert result["namespace"] == "production"
    assert result["service_name"] is None
    assert result["severity"] == "critical"
    assert result["triage_status"] == "TRIAGED"
    assert result["status"] == "TRIAGED"


@pytest.mark.asyncio
async def test_triage_node_ignores_control_plane_resources():
    """Verify triage_node drops internal NEXUS/kube-system events to prevent self-healing loops."""
    state: IncidentState = {
        "incident_id": "inc-cp-01",
        "namespace": "nexus",
        "service_name": "nexus-api",
        "raw_events": [{"namespace": "nexus", "service_name": "nexus-api"}],
    }

    result = await triage_node(state)
    assert result["triage_status"] == "IGNORED"
    assert result["status"] == "IGNORED"


def test_incident_state_propagation_in_graph_execution():
    """Verify IncidentState TypedDict propagates discovered topology across graph nodes."""
    workflow = StateGraph(IncidentState)

    def node_a(state: IncidentState):
        return {"namespace": "shop-ns", "service_name": "shop-demo"}

    def node_b(state: IncidentState):
        return {
            "target_resource": f"{state['namespace']}/Deployment/{state['service_name']}",
            "status": "DIAGNOSED",
        }

    workflow.add_node("node_a", node_a)
    workflow.add_node("node_b", node_b)
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.set_finish_point("node_b")

    app = workflow.compile()
    final_state = app.invoke({"incident_id": "inc-prop-01", "namespace": None})
    assert final_state["target_resource"] == "shop-ns/Deployment/shop-demo"
    assert final_state["status"] == "DIAGNOSED"


def test_planned_tool_step_and_verification_condition_contracts():
    """Verify PlanStep, PlannedToolStep alias, and VerificationCondition contracts."""
    verif = VerificationCondition(
        metric_name="http_5xx_rate",
        operator="<",
        threshold=0.01,
        window_seconds=120,
    )

    step = PlanStep(
        step_index=0,
        action_tool="aws_dynamodb_update_table",
        action_args={"table_name": "orders", "write_capacity_units": 250},
        description="Scale up WCU to stop write throttles",
        rollback_tool="aws_dynamodb_update_table",
        rollback_args={"table_name": "orders", "write_capacity_units": 50},
        verification_tool="aws_get_metric_data",
        verification_args={
            "namespace": "AWS/DynamoDB",
            "metric_name": "SuccessfulRequestLatency",
        },
        verification_condition=verif,
    )

    assert step.tool_name == "aws_dynamodb_update_table"
    assert step.parameters["write_capacity_units"] == 250
    assert step.rollback_parameters["write_capacity_units"] == 50
    assert step.verification_condition.operator == "<"
    assert step.verification_condition.threshold == 0.01
    assert PlannedToolStep is PlanStep
