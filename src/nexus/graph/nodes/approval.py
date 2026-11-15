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
    if state.get("retry_count", 0) > 0 and len(state.get("execution_records", [])) > 0:
        decision = None

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
            "prompt": "Operator action required to execute mutating remediation plan.",
        }
        # Calling interrupt halts execution until resume_incident() provides input
        decision = interrupt(interrupt_payload)

    # Process verdict
    now_iso = datetime.now(timezone.utc).isoformat()
    action = "approved"
    edited_plan = None

    if isinstance(decision, dict):
        raw_action = str(
            decision.get("action") or decision.get("decision") or "approved"
        ).lower()
        if raw_action in ("approved", "yes", "true", "proceed"):
            action = "approved"
        elif raw_action in ("edit", "modify"):
            action = "edit"
        else:
            action = "rejected"

        if "plan" in decision:
            edited_plan = decision["plan"]
        elif "parameters" in decision or "steps" in decision:
            curr_plan = dict(state.get("plan") or {})
            if "steps" in decision:
                curr_plan["steps"] = decision["steps"]
            elif "parameters" in decision and curr_plan.get("steps"):
                st_list = [
                    dict(s) if isinstance(s, dict) else s.model_dump()
                    for s in curr_plan["steps"]
                ]
                st_list[0]["parameters"] = decision["parameters"]
                curr_plan["steps"] = st_list
            edited_plan = curr_plan
    else:
        decision_str = str(decision).lower()
        if decision_str in ("approved", "yes", "true", "proceed"):
            action = "approved"
        elif decision_str in ("edit", "modify"):
            action = "edit"
        else:
            action = "rejected"

    res_payload: dict[str, Any] = {
        "approval_id": incident_id,
        "approval_decision": action,
    }

    if action == "approved":
        logger.info("[Approval] Incident %s APPROVED by human operator", incident_id)
        res_payload["fsm_state"] = IncidentState.EXECUTING.value
        res_payload["audit_log"] = [
            {
                "timestamp": now_iso,
                "stage": "approval",
                "action": "approved",
                "details": "Operator authorized plan execution",
            }
        ]
    elif action == "edit":
        logger.info(
            "[Approval] Incident %s EDITED by human operator — routing back to govern",
            incident_id,
        )
        res_payload["fsm_state"] = IncidentState.POLICY_CHECK.value
        if edited_plan is not None:
            res_payload["plan"] = edited_plan
        res_payload["audit_log"] = [
            {
                "timestamp": now_iso,
                "stage": "approval",
                "action": "edited",
                "details": f"Operator edited plan arguments: {edited_plan or decision}",
            }
        ]
    else:
        logger.warning("[Approval] Incident %s REJECTED by human operator", incident_id)
        res_payload["fsm_state"] = IncidentState.REJECTED.value
        res_payload["audit_log"] = [
            {
                "timestamp": now_iso,
                "stage": "approval",
                "action": "rejected",
                "details": f"Operator rejected remediation: {decision}",
            }
        ]

    return res_payload
