"""
StateGraph construction and compilation for NEXUS incidents.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from nexus.graph.nodes import (
    approval_node,
    collect_telemetry_node,
    diagnose_node,
    escalate_node,
    execute_node,
    govern_node,
    learn_node,
    plan_remediation_node,
    rollback_node,
    triage_node,
    verify_node,
)
from nexus.graph.routes import (
    route_after_approval,
    route_after_execute,
    route_after_govern,
    route_after_verify,
)
from nexus.graph.state import IncidentGraphState


def build_incident_graph(checkpointer: Any = None) -> Any:
    """Construct and compile the unified NEXUS incident response StateGraph."""
    workflow = StateGraph(IncidentGraphState)

    # 1. Register Nodes
    workflow.add_node("triage", triage_node)
    workflow.add_node("collect", collect_telemetry_node)
    workflow.add_node("diagnose", diagnose_node)
    workflow.add_node("plan", plan_remediation_node)
    workflow.add_node("govern", govern_node)
    workflow.add_node("approval", approval_node)
    workflow.add_node("execute", execute_node)
    workflow.add_node("verify", verify_node)
    workflow.add_node("rollback", rollback_node)
    workflow.add_node("learn", learn_node)
    workflow.add_node("escalate", escalate_node)

    # 2. Linear Entry & Analysis Edges
    workflow.add_edge(START, "triage")
    workflow.add_edge("triage", "collect")
    workflow.add_edge("collect", "diagnose")
    workflow.add_edge("diagnose", "plan")
    workflow.add_edge("plan", "govern")

    # 3. Governance Conditional Branching
    workflow.add_conditional_edges(
        "govern",
        route_after_govern,
        {
            "execute": "execute",
            "approval": "approval",
            "escalate": "escalate",
            "learn": "learn",
        },
    )

    # 4. Approval Conditional Branching
    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "execute": "execute",
            "escalate": "escalate",
        },
    )

    # 5. Execution Conditional Branching
    workflow.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "verify": "verify",
            "rollback": "rollback",
        },
    )

    # 6. Verification Conditional Branching
    workflow.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "learn": "learn",
            "plan": "plan",
            "rollback": "rollback",
        },
    )

    # 7. Rollback & Terminal Edges
    workflow.add_edge("rollback", "escalate")
    workflow.add_edge("learn", END)
    workflow.add_edge("escalate", END)

    return workflow.compile(checkpointer=checkpointer)
