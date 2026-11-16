"""
Unit tests for NEXUS IncidentGraphState backward compatibility,
AnomalySignal model serialization, and triage ingestion boundary.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph

from nexus.bus.events.event import AnomalySignal
from nexus.db.models import IncidentState
from nexus.graph.builder import build_incident_graph
from nexus.graph.nodes.triage import triage_node
from nexus.graph.state import (
    IncidentGraphState,
    PlannedToolStep,
    PlanStep,
    VerificationCondition,
)
from nexus.graph.workflow_events import build_initial_state


def test_incident_graph_state_compilation():
    """Verify StateGraph compiles cleanly with extended IncidentGraphState."""
    graph = build_incident_graph()
    assert graph is not None


def test_build_initial_state_preserves_fsm_and_new_extensions():
    """Verify build_initial_state preserves legacy FSM fields and initializes autonomous extensions."""
    data = {
        "events": [
            {"agent": "k8s", "severity": "warning", "resource_name": "checkout"}
        ],
        "platform": "kubernetes",
    }
    state = build_initial_state(data, inc_id="inc-test-01")

    # Legacy fields
    assert state["incident_id"] == "inc-test-01"
    assert state["fsm_state"] == IncidentState.DETECTED.value
    assert len(state["events"]) == 1
    assert state["platform"] == "kubernetes"
    assert state["telemetry"] == {}
    assert state["diagnosis"] == {}
    assert state["plan"] is None
    assert state["governance"] is None
    assert state["execution_records"] == []
    assert state["retry_count"] == 0
    assert state["resolved"] is False
    assert state["escalated"] is False

    # Autonomous extensions
    assert state["anomaly_signal"] is None
    assert state["agentic_rca"] is None
    assert state["fallback_active"] is False
    assert state["messages"] == []


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

    # NATS payload serialization roundtrip
    payload = signal.to_nats_payload()
    deserialized = AnomalySignal.from_nats_payload(payload)
    assert deserialized.signal_id == signal.signal_id
    assert deserialized.target_service == "checkout-service"
    assert deserialized.raw_metrics_or_alert["wcu"] == 50


def test_triage_node_with_legacy_events():
    """Verify triage_node maintains full backward compatibility for standard IncidentEvent streams."""
    state = {
        "incident_id": "inc-legacy-01",
        "fsm_state": IncidentState.DETECTED.value,
        "events": [
            {
                "agent": "k8s",
                "resource_name": "payment-api",
                "namespace": "production",
                "severity": "critical",
                "signal_type": "pod_oomkilled",
            }
        ],
        "anomaly_signal": None,
    }

    result = triage_node(state)
    assert result["platform"] == "kubernetes"
    assert result["target"]["name"] == "payment-api"
    assert result["severity"] == "critical"
    assert result["fsm_state"] == IncidentState.CORRELATED.value
    assert result["fallback_active"] is False
    assert len(result["audit_log"]) == 1


def test_triage_node_with_anomaly_signal():
    """Verify triage_node correctly extracts target, platform, and initializes messages for AnomalySignal."""
    signal = AnomalySignal(
        source="ppa_forecaster",
        provider="aws",
        environment="production",
        target_service="orders-table",
        symptom_summary="Write capacity saturated causing HTTP 500 spike",
        raw_metrics_or_alert={"throttles": 340},
    )

    state = {
        "incident_id": "inc-anomaly-01",
        "fsm_state": IncidentState.DETECTED.value,
        "events": [],
        "anomaly_signal": signal,
    }

    result = triage_node(state)
    assert result["platform"] == "aws"
    assert result["target"]["name"] == "orders-table"
    assert result["target"]["namespace"] == "production"
    assert result["severity"] == "critical"
    assert result["fsm_state"] == IncidentState.CORRELATED.value
    assert result["fallback_active"] is False
    assert "messages" in result
    assert len(result["messages"]) == 1
    assert "AnomalySignal" in result["messages"][0].content
    assert "orders-table" in result["messages"][0].content


def test_messages_add_reducer_in_graph_execution():
    """Verify add_messages reducer appends turns during LangGraph execution without overwriting."""
    workflow = StateGraph(IncidentGraphState)

    def node_a(state: IncidentGraphState):
        return {"messages": [HumanMessage(content="User observation 1")]}

    def node_b(state: IncidentGraphState):
        return {"messages": [AIMessage(content="Agent analysis 1")]}

    workflow.add_node("node_a", node_a)
    workflow.add_node("node_b", node_b)
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.set_finish_point("node_b")

    app = workflow.compile()
    initial_state = {
        "incident_id": "inc-reducer-01",
        "fsm_state": IncidentState.DETECTED.value,
        "events": [],
        "platform": "kubernetes",
        "target": {},
        "severity": "warning",
        "telemetry": {},
        "diagnosis": {},
        "diagnostic_reflections": [],
        "plan": None,
        "remediation_history": [],
        "governance": None,
        "approval_id": None,
        "approval_decision": None,
        "execution_records": [],
        "rollback_executed": False,
        "rollback_records": [],
        "verification": None,
        "retry_count": 0,
        "max_retries": 2,
        "audit_log": [],
        "messages": [HumanMessage(content="Seed message")],
        "error_message": None,
        "resolved": False,
        "escalated": False,
        "anomaly_signal": None,
        "agentic_rca": None,
        "fallback_active": False,
    }

    final_state = app.invoke(initial_state)
    assert len(final_state["messages"]) == 3
    assert final_state["messages"][0].content == "Seed message"
    assert final_state["messages"][1].content == "User observation 1"
    assert final_state["messages"][2].content == "Agent analysis 1"


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
