import logging
from typing import Any

from langgraph.types import interrupt

from nexus.engine.workflow.state import IncidentState
from nexus.engine.workflow.workflow_state import sync_discovered_target_identity

logger = logging.getLogger(__name__)


async def approval_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    current_status = state.get("approval_status")

    if current_status == "AUTO_APPROVED" or (
        current_status == "APPROVED" and state.get("approved") is True
    ):
        return {
            "approved": True,
            "approval_status": current_status or "APPROVED",
            "status": "APPROVED",
        }

    if current_status == "REJECTED" and state.get("approved") is False:
        return {
            "approved": False,
            "approval_status": "REJECTED",
            "status": "ESCALATED",
        }

    await sync_discovered_target_identity(incident_id, state)

    logger.info(
        "Incident %s pausing at approval_node for human operator decision.", incident_id
    )
    resume_value = interrupt(
        {
            "incident_id": incident_id,
            "namespace": state.get("namespace"),
            "service_name": state.get("service_name"),
            "target_resource": state.get("target_resource"),
            "failure_class": state.get("failure_class"),
            "suspected_cause": state.get("suspected_cause"),
            "remediation_plan": state.get("remediation_plan"),
        }
    )

    if isinstance(resume_value, dict):
        approved = bool(resume_value.get("approved"))
        approver = resume_value.get("approved_by", "slack-operator")
        reason = resume_value.get("approval_reason", "")
    else:
        approved = bool(resume_value)
        approver = "slack-operator"
        reason = ""

    return {
        "approved": approved,
        "approval_status": "APPROVED" if approved else "REJECTED",
        "approved_by": approver,
        "approval_reason": reason,
        "status": "APPROVED" if approved else "ESCALATED",
    }
