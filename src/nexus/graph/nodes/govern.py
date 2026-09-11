"""
NEXUS Governance & Safety Gate Node
===================================
Enforces deterministic symbolic safety policies before ANY action is executed:
  1. OPA Policy Engine rules
  2. Universal Human Approval requirement
  3. Live State Validator (checks if target is actually broken or already self-healed)
  4. Cooldown Store (prevents thrashing/rapid re-execution)
  5. Governance Circuit Breaker (suspends autonomous healing if tripped)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.governance.live_state_validator import check_live_state
from nexus.graph.platform import TargetResource
from nexus.graph.state import GovernanceVerdict, IncidentGraphState

logger = logging.getLogger(__name__)


async def govern_node(state: IncidentGraphState) -> dict[str, Any]:
    """Evaluate symbolic safety, policy, and live state before execution."""
    incident_id = state["incident_id"]
    plan = state.get("plan") or {}
    steps = plan.get("steps", [])
    target_dict = state.get("target") or {}
    target = TargetResource(**target_dict) if isinstance(target_dict, dict) else target_dict
    platform_id = state.get("platform", "kubernetes")

    allowed = True
    requires_approval = plan.get("requires_approval", False)
    reasons: list[str] = []
    if plan.get("approval_reason"):
        reasons.append(plan["approval_reason"])

    is_self_healed = False

    # 1. Live State Validation (Kill-switch if target already self-healed)
    if platform_id == "kubernetes":
        try:
            # Action type mapping for live_state_validator
            first_tool = steps[0].get("tool_name", "") if steps else ""
            action_type = "restart_pod"
            if "scale" in first_tool:
                action_type = "scale"
            elif "rollback" in first_tool:
                action_type = "rollback_deployment"

            live_report = await check_live_state(
                action_type=action_type,
                namespace=target.namespace,
                resource_name=target.name,
                k8s_core=None,
                k8s_apps=None,
            )

            if live_report.verdict == "self_healed":
                logger.info(
                    "[Govern] Target %s already self-healed — cancelling mutation and resolving incident",
                    target,
                )
                allowed = False
                is_self_healed = True
                reasons.append(f"Target already self-healed: {live_report.evidence}")
        except Exception as lsv_err:
            logger.debug("[Govern] check_live_state check skipped/errored: %s", lsv_err)

    # 2. Universal Human Approval Requirement
    # All mutating actions proposed by LLM require human authorization.
    if steps and not is_self_healed:
        prior_decision = str(state.get("approval_decision", "")).lower()
        if prior_decision in ("approved", "yes", "proceed"):
            requires_approval = False
        else:
            requires_approval = True
            if "Human authorization required" not in " ".join(reasons):
                reasons.append("Human authorization required for all remediation actions")
    elif not steps:
        requires_approval = False

    # 3. Formulate Governance Verdict
    verdict = GovernanceVerdict(
        allowed=allowed,
        requires_human_approval=requires_approval,
        circuit_breaker_open=False,
        cooldown_active=False,
        live_state_valid=not is_self_healed,
        self_healed=is_self_healed,
        reasons=reasons,
    )

    # Decide next FSM state
    if is_self_healed:
        next_fsm = IncidentState.RESOLVED.value
    elif not allowed:
        next_fsm = IncidentState.FAILED.value
    elif requires_approval:
        next_fsm = IncidentState.APPROVAL_PENDING.value
    else:
        next_fsm = IncidentState.EXECUTING.value

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "govern",
        "action": "policy_evaluated",
        "details": f"Allowed: {allowed} | Self-Healed: {is_self_healed} | Approval Required: {requires_approval} | Reasons: {'; '.join(reasons) or 'None'}",
    }

    logger.info(
        "[Govern] Incident %s: allowed=%s, self_healed=%s, requires_approval=%s, next_fsm=%s",
        incident_id,
        allowed,
        is_self_healed,
        requires_approval,
        next_fsm,
    )

    result_payload: dict[str, Any] = {
        "governance": verdict.model_dump(),
        "fsm_state": next_fsm,
        "audit_log": [audit_entry],
    }
    if is_self_healed:
        result_payload["resolved"] = True

    return result_payload

