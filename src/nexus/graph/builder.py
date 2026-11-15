"""
StateGraph construction and compilation for NEXUS incidents.
=============================================================
Assembles the provider-agnostic, multi-turn ReAct investigator, schema-driven
planner, attribute-based OPA gate, and closed-loop verification pipeline.
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from nexus.graph.nodes import (
    approval_node,
    diagnose_node,
    escalate_node,
    execute_node,
    govern_node,
    learn_node,
    plan_node,
    rca_reasoner_node,
    rca_tool_executor_node,
    rollback_node,
    triage_node,
    verify_node,
)
from nexus.graph.nodes.collect import collect_telemetry_node
from nexus.graph.routes import (
    route_after_approval,
    route_after_diagnose,
    route_after_govern,
    route_after_rca_reasoner,
    route_after_verify,
)
from nexus.graph.state import IncidentGraphState


def build_nexus_agent_graph(
    nodes: dict[str, Any] | None = None, checkpointer: Any = None
) -> Any:
    """Build and compile the governed NEXUS agentic incident StateGraph."""
    graph = StateGraph(IncidentGraphState)
    node_map = {
        "triage": triage_node,
        "rca_reasoner": rca_reasoner_node,
        "rca_tool_executor": rca_tool_executor_node,
        "diagnose": diagnose_node,
        "collect": collect_telemetry_node,
        "plan": plan_node,
        "govern": govern_node,
        "approval": approval_node,
        "execute": execute_node,
        "verify": verify_node,
        "rollback": rollback_node,
        "learn": learn_node,
        "escalate": escalate_node,
    }
    if nodes:
        node_map.update(nodes)

    for name, fn in node_map.items():
        cast(Any, graph).add_node(name, fn)

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "diagnose")
    graph.add_conditional_edges(
        "diagnose",
        route_after_diagnose,
        {
            "plan": "plan",
            "collect": "collect",
        },
    )
    graph.add_edge("collect", "plan")

    # Connect multi-turn reasoning sub-loop if targeted
    graph.add_conditional_edges(
        "rca_reasoner",
        route_after_rca_reasoner,
        {
            "rca_tool_executor": "rca_tool_executor",
            "plan": "plan",
            "learn": "learn",
        },
    )
    graph.add_edge("rca_tool_executor", "rca_reasoner")

    graph.add_edge("plan", "govern")
    graph.add_conditional_edges(
        "govern",
        route_after_govern,
        {
            "execute": "execute",
            "approval": "approval",
            "escalate": "escalate",
            "learn": "learn",
        },
    )
    graph.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "execute": "execute",
            "govern": "govern",
            "escalate": "escalate",
        },
    )
    graph.add_edge("execute", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "learn": "learn",
            "rollback": "rollback",
            "plan": "plan",
        },
    )
    graph.add_edge("rollback", "escalate")
    graph.add_edge("learn", END)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer)


def build_incident_graph(checkpointer: Any = None) -> Any:
    """Compatibility builder for existing test harnesses and workflows."""
    return build_nexus_agent_graph(checkpointer=checkpointer)
