"""
NEXUS Human-in-the-Loop Approval Node
======================================
Pauses execution via LangGraph native interrupt() when an action requires
human authorization (L2/L3 actions or confidence below threshold).
Resumes when an SRE approves or rejects the action via Slack, Dashboard, or API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.types import interrupt

from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


def approval_node(state: IncidentGraphState) -> dict[str, Any]:
    """Human approval gate using LangGraph interrupt()."""
    incident_id = state["incident_id"]
    decision = state.get("approval_decision")

    # If decision was not pre-populated, pause execution via LangGraph interrupt
    if not decision:
        logger.info(
            "[Approval] Pausing incident %s for human authorization (L2/L3 or low confidence)",
            incident_id,
        )
        interrupt_payload = {
            "incident_id": incident_id,
            "target": state.get("target"),
            "platform": state.get("platform"),
            "plan": state.get("plan"),
            "governance_reasons": (state.get("governance") or {}).get("reasons", []),
            "prompt": "Operator approval required to execute mutating remediation plan.",
        }
        # Calling interrupt halts execution until resume_incident() provides input
        decision = interrupt(interrupt_payload)

    # Process verdict
    approved = str(decision).lower() in ("approved", "yes", "true", "proceed")
    now_iso = datetime.now(timezone.utc).isoformat()

    if approved:
        logger.info("[Approval] Incident %s APPROVED by human operator", incident_id)
        next_fsm = IncidentState.EXECUTING.value
        audit_entry = {
            "timestamp": now_iso,
            "stage": "approval",
            "action": "approved",
            "details": "Operator authorized plan execution",
        }
    else:
        logger.warning("[Approval] Incident %s REJECTED by human operator", incident_id)
        next_fsm = IncidentState.REJECTED.value
        audit_entry = {
            "timestamp": now_iso,
            "stage": "approval",
            "action": "rejected",
            "details": f"Operator rejected remediation: {decision}",
        }

    return {
        "approval_id": incident_id,
        "approval_decision": "approved" if approved else "rejected",
        "fsm_state": next_fsm,
        "audit_log": [audit_entry],
    }
