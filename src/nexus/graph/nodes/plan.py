"""
NEXUS Remediation Planner Node
==============================
Translates the diagnosed root cause and telemetry into a typed, structured
RemediationPlan using registered write tools for the target platform.
Pre-calculates risk tiers and rollback actions deterministically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.nodes.action_planner import build_suggested_action_steps
from nexus.graph.nodes.adaptive_recovery import build_adaptive_recovery_steps
from nexus.graph.nodes.default_planner import (
    build_aws_default_steps,
    build_k8s_default_steps,
    build_pluggable_platform_steps,
)
from nexus.graph.state import IncidentGraphState, RemediationPlan

logger = logging.getLogger(__name__)


def plan_remediation_node(state: IncidentGraphState) -> dict[str, Any]:
    """Generate structured remediation plan for target platform."""
    incident_id = state["incident_id"]
    diagnosis = state.get("diagnosis", {})
    target = state.get("target", {})
    platform = state.get("platform", "kubernetes")
    failure_class = diagnosis.get("failure_class", "unknown")
    confidence = float(diagnosis.get("confidence", 0.8))

    target_name = target.get("name", "unknown")
    target_ns = target.get("namespace", "default")

    retry_count = state.get("retry_count", 0)
    verification = state.get("verification") or {}
    execution_records = state.get("execution_records") or []
    prior_tools: list[str] = [
        str(rec["tool_name"]) for rec in execution_records if rec.get("tool_name")
    ]
    verif_failure = (
        verification.get("failure_reason")
        or verification.get("details")
        or "Target remained unhealthy"
    )

    suggested_action = diagnosis.get("suggested_action")

    # 1. Adaptive recovery ladder for retries
    if retry_count > 0 and prior_tools:
        logger.info(
            "[Plan Adaptive Ladder] Retry attempt %d for incident %s. Prior tools: %s. Failure: %s",
            retry_count,
            incident_id,
            prior_tools,
            verif_failure,
        )
        steps, approval_reason = build_adaptive_recovery_steps(
            platform=platform,
            failure_class=failure_class,
            target_name=target_name,
            target_ns=target_ns,
            retry_count=retry_count,
            prior_tools=prior_tools,
            verif_failure=verif_failure,
            diagnosis=diagnosis,
            state=state,
        )
    elif suggested_action:
        steps, approval_reason = build_suggested_action_steps(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            suggested_action=suggested_action,
            diagnosis=diagnosis,
            state=state,
        )
    elif platform == "kubernetes":
        steps, approval_reason = build_k8s_default_steps(
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            diagnosis=diagnosis,
            state=state,
        )
    elif platform == "aws":
        steps, approval_reason = build_aws_default_steps(
            target_name=target_name,
            target_ns=target_ns,
            failure_class=failure_class,
            diagnosis=diagnosis,
            state=state,
        )
    else:
        steps, approval_reason = build_pluggable_platform_steps(
            platform=platform,
            target_name=target_name,
            target_ns=target_ns,
            target=target,
        )

    requires_approval = bool(steps)
    if requires_approval and not approval_reason:
        approval_reason = f"Human authorization required to execute {steps[0].tool_name} on {target_name}"

    plan = RemediationPlan(
        incident_id=incident_id,
        root_cause=diagnosis.get("root_cause", "Unspecified failure"),
        failure_mode=failure_class,
        confidence=confidence,
        steps=steps,
        requires_approval=requires_approval,
        approval_reason=approval_reason,
        pre_flight_conditions={"target_exists": True},
        expected_slo_target="error_rate < 0.01",
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "plan",
        "action": "plan_generated",
        "details": f"Steps: {len(steps)} | Requires Approval: {requires_approval} ({approval_reason or 'autonomous'})",
    }

    logger.info(
        "[Plan] Incident %s planned: %d step(s), approval=%s",
        incident_id,
        len(steps),
        requires_approval,
    )

    remediation_entry = {
        "attempt": retry_count,
        "prior_tools": prior_tools,
        "post_check_failure": verif_failure if retry_count > 0 else None,
        "proposed_steps": [s.model_dump() for s in steps],
        "is_adaptive_escalation": retry_count > 0,
    }

    result_dict: dict[str, Any] = {
        "plan": plan.model_dump(),
        "remediation_history": [remediation_entry],
        "fsm_state": IncidentState.POLICY_CHECK.value,
        "audit_log": [audit_entry],
    }
    if retry_count > 0:
        result_dict["approval_decision"] = None
    return result_dict
