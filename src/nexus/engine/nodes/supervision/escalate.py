"""
NEXUS Escalation Node
=====================
Persists discovered target identity, derives a clear human-readable escalation
reason (distinct from RCA summary), and dispatches a single rich Slack alert
when running outside the NATS Notifier lifecycle pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from nexus.engine.workflow.state.state import IncidentState
from nexus.engine.workflow.state.workflow_state import (
    sync_discovered_target_identity,
)

logger = logging.getLogger(__name__)


def derive_escalation_reason(state: Mapping[str, Any]) -> str:
    """Return a clear, specific explanation of *why* the incident escalated."""
    policy_decision = state.get("policy_decision") or {}
    status = str(state.get("status") or "")

    # 1. Check OPA policy denial FIRST (never confuse with human rejection)
    if status == "POLICY_DENIED" or policy_decision.get("allowed") is False:
        reason = policy_decision.get("reason") or "Action blocked by zero-trust policy"
        return f"OPA Policy Denied: {reason}"

    err = state.get("error")
    if err and str(err).strip() and str(err).strip().upper() != "ESCALATED":
        return str(err).strip()

    # 2. Check genuine human rejection at approval_node
    if state.get("approval_status") == "REJECTED" and state.get("approved") is False:
        operator = state.get("approved_by") or "Operator"
        detail = (
            state.get("approval_reason") or "Operator rejected proposed remediation"
        )
        return f"Human approval rejected by {operator}: {detail}"

    if state.get("verification_passed") is False and state.get("verification_feedback"):
        return f"Post-remediation verification failed: {state['verification_feedback']}"

    if state.get("execution_status") == "FAILED":
        return "Remediation execution failed"

    rca = state.get("rca_result") or {}
    plan_steps = (state.get("remediation_plan") or {}).get("steps") or []
    min_conf = float(state.get("min_confidence_threshold", 0.65))
    confidence = float(state.get("confidence") or rca.get("confidence") or 0.0)

    if status == "UNPLANNABLE" or (
        rca and rca.get("can_auto_remediate") and not plan_steps
    ):
        rationale = (state.get("remediation_plan") or {}).get("rationale")
        if rationale:
            return f"Planner produced no safe mutation steps: {rationale}"
        return "Planner could not bind a safe remediation tool call for the diagnosed root cause"

    if rca:
        if not rca.get("can_auto_remediate", False):
            return (
                f"Auto-remediation disabled by investigator "
                f"(can_auto_remediate=False, confidence={confidence:.0%})"
            )
        if confidence < min_conf:
            return (
                f"RCA confidence ({confidence:.0%}) is below auto-remediation "
                f"threshold ({min_conf:.0%})"
            )

    if state.get("escalation_reason"):
        return str(state["escalation_reason"])
    if state.get("approval_reason"):
        return str(state["approval_reason"])
    if policy_decision.get("reason"):
        return str(policy_decision["reason"])

    return "Escalated for manual SRE review"


def _nats_notifier_active() -> bool:
    """Return True when NATS is connected so Notifier handles lifecycle escalations."""
    try:
        from nexus.core.bus.nats.client import NATSClient

        inst = getattr(NATSClient, "_instance", None)
        return bool(inst is not None and getattr(inst, "is_connected", False))
    except Exception:
        return False


async def escalate_node(state: IncidentState) -> dict[str, Any]:
    """Persist discovered identity and escalate incident to human engineering team."""
    incident_id = state.get("incident_id", "unknown")
    platform = state.get("platform") or "unknown"
    target_ns = state.get("namespace") or "default"
    target_resource = (
        state.get("target_resource")
        or (
            f"{state.get('namespace')}/{state.get('resource_kind') or 'Resource'}/{state.get('service_name')}"
            if state.get("namespace") and state.get("service_name")
            else state.get("service_name")
        )
        or "unresolved-target"
    )
    rca = state.get("rca_result") or {}
    plan = state.get("remediation_plan") or {}
    failure_class = (
        state.get("failure_class")
        or rca.get("failure_type")
        or rca.get("failure_class")
        or "unclassified"
    )
    root_cause = (
        rca.get("root_cause_summary")
        or state.get("suspected_cause")
        or rca.get("root_cause")
        or "Unknown"
    )
    recommended_fix = rca.get("recommended_strategy") or plan.get("rationale") or None
    evidence = rca.get("evidence") or None
    proposed_steps = plan.get("steps") or None
    confidence = float(state.get("confidence") or rca.get("confidence") or 0.0) or None
    reason = derive_escalation_reason(state)
    rollback_applied = bool(state.get("rollback_results"))

    # Sync discovered identity to DB before alerts/completion
    await sync_discovered_target_identity(incident_id, state)

    logger.warning(
        "Incident %s ESCALATED target=%s platform=%s failure_class=%s reason=%s recommended_fix=%s",
        incident_id,
        target_resource,
        platform,
        failure_class,
        reason,
        (recommended_fix or "")[:160],
    )

    # Avoid duplicate Slack alerts: when NATS is connected, handle_workflow_completion()
    # publishes `nexus.lifecycle.escalated` and Notifier sends the single canonical card.
    # Only send directly here if running standalone without NATS.
    if not _nats_notifier_active():
        try:
            from nexus.services.notifications.dispatcher.notifier import Notifier

            notifier = Notifier()
            await notifier.notify_incident_escalated(
                app_name=str(target_ns),
                incident_id=incident_id,
                target=str(target_resource),
                reason=reason,
                failure_class=str(failure_class),
                root_cause=str(root_cause),
                confidence=confidence,
                recommended_fix=recommended_fix,
                evidence=evidence,
                proposed_steps=proposed_steps,
                rollback_applied=rollback_applied,
            )
        except Exception as exc:
            logger.debug("[Escalate] Standalone Slack notification skipped: %s", exc)

    return {
        "status": "ESCALATED",
        "error": reason,
    }
