import logging
from typing import Any

from nexus.engine.workflow.state import IncidentState
from nexus.governance.policies.policy_engine import evaluate_opa_policy

logger = logging.getLogger(__name__)


async def govern_node(state: IncidentState) -> dict[str, Any]:
    incident_id = state.get("incident_id", "unknown")
    plan = state.get("remediation_plan") or {}
    steps = plan.get("steps") or []

    if not steps:
        return {
            "policy_decision": {
                "allowed": False,
                "requires_approval": False,
                "reason": "Empty remediation plan",
            },
            "requires_approval": False,
            "approved": None,
            "approval_status": "POLICY_DENIED",
            "status": "POLICY_DENIED",
        }

    opa_input = {
        "incident_id": incident_id,
        "platform": state.get("platform"),
        "namespace": state.get("namespace"),
        "resource_kind": state.get("resource_kind"),
        "service_name": state.get("service_name"),
        "confidence": float(state.get("confidence", 0.0)),
        "remediation_attempts": state.get("remediation_attempts", 0),
        "steps": [
            {
                "tool_name": s["tool_name"],
                "tool_args": s["tool_args"],
                "blast_radius": s.get("blast_radius", "medium"),
                "reversible": s.get("reversible", True),
            }
            for s in steps
        ],
    }

    decision = await evaluate_opa_policy(opa_input)
    allowed = bool(decision.get("allowed", True))
    requires_approval = bool(decision.get("requires_approval", True))

    if not allowed:
        logger.warning(
            "Incident %s blocked by OPA policy: %s", incident_id, decision.get("reason")
        )
        return {
            "policy_decision": decision,
            "requires_approval": False,
            "approved": None,
            "approval_status": "POLICY_DENIED",
            "status": "POLICY_DENIED",
        }

    if requires_approval:
        return {
            "policy_decision": decision,
            "requires_approval": True,
            "approved": None,
            "approved_by": None,
            "approval_reason": None,
            "approval_status": "PENDING",
            "status": "AWAITING_APPROVAL",
        }

    return {
        "policy_decision": decision,
        "requires_approval": False,
        "approved": True,
        "approval_status": "AUTO_APPROVED",
        "approved_by": "opa-policy-engine",
        "status": "APPROVED",
    }
