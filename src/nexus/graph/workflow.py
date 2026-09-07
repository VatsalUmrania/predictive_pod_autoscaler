"""
NEXUS Unified LangGraph Incident Workflow
=========================================
Builds, compiles, and orchestrates the governed incident response StateGraph.
Replaces basic ReAct loops with a deterministic Plan-Validate-Execute-Verify-Rollback
state machine integrated with PostgreSQL/Memory checkpointers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from nexus.engine.fsm import IncidentState
from nexus.graph.checkpointer import GraphCheckpointStore
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
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


# ── Conditional Routing Functions ─────────────────────────────────────────────

def route_after_govern(state: IncidentGraphState) -> Literal["execute", "approval", "escalate", "learn"]:
    """Route based on symbolic governance, OPA policies, and risk tier."""
    gov = state.get("governance") or {}
    allowed = gov.get("allowed", True)
    requires_approval = gov.get("requires_human_approval", False)
    self_healed = gov.get("self_healed", False) or state.get("resolved", False)

    if self_healed:
        logger.info("[Workflow Router] Target self-healed before execution — routing directly to learn")
        return "learn"

    if not allowed:
        logger.warning("[Workflow Router] Governance BLOCKED plan — routing to escalate")
        return "escalate"

    if requires_approval:
        logger.info("[Workflow Router] Plan requires human approval — routing to approval")
        return "approval"

    logger.info("[Workflow Router] Plan approved — routing to execute")
    return "execute"



def route_after_approval(state: IncidentGraphState) -> Literal["execute", "escalate"]:
    """Route based on human operator verdict."""
    decision = state.get("approval_decision", "")
    if str(decision).lower() in ("approved", "yes", "proceed"):
        return "execute"
    return "escalate"


def route_after_execute(state: IncidentGraphState) -> Literal["verify", "rollback"]:
    """Route based on execution success or failure."""
    if state.get("fsm_state") == IncidentState.ROLLING_BACK.value or state.get("error_message"):
        return "rollback"
    return "verify"


def route_after_verify(state: IncidentGraphState) -> Literal["learn", "plan", "rollback"]:
    """
    Route based on post-check verification:
      - Resolved & healthy -> learn (success path)
      - Unhealthy & retries remaining -> plan (retry path)
      - Unhealthy & max retries reached -> rollback (failure path)
    """
    if state.get("resolved") or state.get("fsm_state") == IncidentState.RESOLVED.value:
        return "learn"

    if state.get("fsm_state") == IncidentState.RETRYING.value:
        logger.info("[Workflow Router] Target unhealthy; retrying remediation")
        return "plan"

    logger.warning("[Workflow Router] Target unhealthy and retries exhausted; rolling back")
    return "rollback"


# ── Graph Builder ─────────────────────────────────────────────────────────────

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


# ── Incident Workflow Controller Class ────────────────────────────────────────

class IncidentWorkflow:
    """High-level interface for executing and managing NEXUS LangGraph incidents."""

    def __init__(self, checkpointer_store: GraphCheckpointStore | None = None) -> None:
        self.store = checkpointer_store or GraphCheckpointStore.from_env()
        self._compiled_graph = build_incident_graph(checkpointer=self.store.saver)

    async def run_incident(
        self,
        incident_data: dict[str, Any],
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Run incident response workflow from initial event signals.

        Args:
            incident_data: Dictionary containing incident_id, events, and optional overrides.
            thread_id: Unique thread identifier for state persistence and interruption.
        """
        inc_id = incident_data.get("incident_id") or str(uuid.uuid4())
        tid = thread_id or inc_id

        events = incident_data.get("events", [])
        if not events and "event" in incident_data:
            events = [incident_data["event"]]

        initial_state: IncidentGraphState = {
            "incident_id": inc_id,
            "fsm_state": IncidentState.DETECTED.value,
            "events": events,
            "platform": incident_data.get("platform", "kubernetes"),
            "target": incident_data.get("target", {}),
            "severity": incident_data.get("severity", "warning"),
            "telemetry": {},
            "diagnosis": {},
            "diagnostic_reflections": [],
            "plan": None,
            "remediation_history": [],
            "governance": None,
            "approval_id": None,
            "approval_decision": incident_data.get("approval_decision"),
            "execution_records": [],
            "rollback_executed": False,
            "rollback_records": [],
            "verification": None,
            "retry_count": 0,
            "max_retries": incident_data.get("max_retries", 2),
            "audit_log": [],
            "messages": [],
            "error_message": None,
            "resolved": False,
            "escalated": False,
        }

        config = {"configurable": {"thread_id": tid}}
        logger.info("[IncidentWorkflow] Starting workflow for incident %s (thread=%s)", inc_id, tid)

        result = await self._compiled_graph.ainvoke(initial_state, config=config)
        return result

    async def resume_incident(
        self,
        thread_id: str,
        approval_decision: str = "approved",
    ) -> dict[str, Any]:
        """Resume an interrupted incident after human authorization."""
        from langgraph.types import Command

        config = {"configurable": {"thread_id": thread_id}}
        logger.info(
            "[IncidentWorkflow] Resuming incident on thread %s with decision '%s'",
            thread_id,
            approval_decision,
        )

        result = await self._compiled_graph.ainvoke(
            Command(resume=approval_decision),
            config=config,
        )
        return result
