"""
Conditional edge routing functions for NEXUS incident response graph.
"""

from __future__ import annotations

import logging
from typing import Literal

from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


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
    """Route based on post-check verification."""
    if state.get("resolved") or state.get("fsm_state") == IncidentState.RESOLVED.value:
        return "learn"

    if state.get("fsm_state") == IncidentState.RETRYING.value:
        logger.info("[Workflow Router] Target unhealthy; retrying remediation")
        return "plan"

    logger.warning("[Workflow Router] Target unhealthy and retries exhausted; rolling back")
    return "rollback"
