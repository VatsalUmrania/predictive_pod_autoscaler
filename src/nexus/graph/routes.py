"""
Conditional edge routing functions for NEXUS incident response graph.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.engine.fsm import IncidentState

logger = logging.getLogger(__name__)


def route_after_rca_reasoner(state: dict[str, Any]) -> str:
    """Route after RCA reasoner: tool executor if incomplete, plan or learn if complete."""
    if state.get("rca_complete"):
        rca = state.get("rca_report") or state.get("diagnosis") or {}
        if not rca.get("requires_remediation", True):
            return "learn"
        return "plan"
    return "rca_tool_executor"


def route_after_diagnose(state: dict[str, Any]) -> str:
    """Backward compatibility route for single-pass diagnose."""
    if state.get("fallback_active"):
        return "collect"
    return "plan"


def route_after_govern(state: dict[str, Any]) -> str:
    """Route based on OPA ABAC decision (ALLOW_AUTO, REQUIRE_APPROVAL, DENY)."""
    gov = state.get("governance") or {}
    decision = state.get("governance_decision") or gov.get("decision")
    allowed = gov.get("allowed", True)
    requires_approval = gov.get("requires_human_approval", False)
    self_healed = gov.get("self_healed", False) or state.get("resolved", False)

    if self_healed:
        return "learn"
    if decision == "DENY" or not allowed:
        return "escalate"
    if not requires_approval:
        return "execute"
    return "approval"


def route_after_approval(state: dict[str, Any]) -> str:
    """Route based on human operator approval verdict."""
    approved = state.get("human_approved")
    decision = str(state.get("approval_decision", "")).lower()
    if approved is True or decision in ("approved", "yes", "true", "proceed"):
        return "execute"
    if decision in ("edit", "modify"):
        return "govern"
    return "escalate"


def route_after_execute(state: dict[str, Any]) -> str:
    """Route after step execution: verify or rollback."""
    if state.get("fsm_state") == IncidentState.ROLLING_BACK.value or state.get(
        "error_message"
    ):
        return "rollback"
    return "verify"


def route_after_verify(state: dict[str, Any]) -> str:
    """Route after verification checks: learn on success, plan on retry, rollback on failure."""
    if state.get("verification_passed") is True or state.get("resolved") is True:
        return "learn"
    if state.get("fsm_state") == IncidentState.RESOLVED.value:
        return "learn"
    if state.get("fsm_state") == IncidentState.RETRYING.value:
        return "plan"
    return "rollback"
