"""
Workflow completion handler and strict Post-Incident Report guard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def finalize_workflow_run(incident_id: str, final_state: dict[str, Any]) -> None:
    """
    Executes terminal workflow reporting.
    Strictly guarded so Post-Incident Reports are NEVER emitted while paused for approval
    or when an incident escalated with 0 executed remediation steps.
    """
    status = final_state.get("status", "UNKNOWN")
    approval_status = final_state.get("approval_status")
    execution_results = final_state.get("execution_results") or []

    if status == "AWAITING_APPROVAL" or approval_status == "PENDING":
        logger.debug(
            "Incident %s is awaiting approval; skipping Post-Incident Report.",
            incident_id,
        )
        return

    if not execution_results:
        logger.info(
            "Incident %s finished with status=%s and 0 executed steps; skipping Post-Incident Report.",
            incident_id,
            status,
        )
        return

    from nexus.services.reporting.incident_report import publish_post_incident_report

    logger.info(
        "Incident %s completed with status=%s and %d executed step(s); publishing Post-Incident Report.",
        incident_id,
        status,
        len(execution_results),
    )
    await publish_post_incident_report(incident_id, final_state)


async def handle_workflow_completion(
    result: dict[str, Any],
    inc_id: str,
    thread_id: str,
    nats: Any = None,
    *,
    publish_report: bool = True,
) -> None:
    """
    Bridge used by IncidentWorkflow: records lifecycle events, persists dashboard
    resolution, and invokes the strictly guarded finalize_workflow_run().
    """
    status = result.get("status", "UNKNOWN")
    approval_status = result.get("approval_status")
    if status == "AWAITING_APPROVAL" or approval_status == "PENDING":
        return

    is_resolved = status == "RESOLVED"
    is_escalated = status in (
        "ESCALATED",
        "POLICY_DENIED",
        "EXECUTION_FAILED",
        "ROLLED_BACK",
    )
    if not (is_resolved or is_escalated):
        return

    target_ns = result.get("namespace") or "unscoped"
    target_name = result.get("service_name") or "unresolved-target"
    target_resource = result.get("target_resource") or f"{target_ns}/{target_name}"
    execution_results = result.get("execution_results") or []
    plan_dict = result.get("remediation_plan") or {}
    steps = plan_dict.get("steps") or []
    action_type = steps[0].get("tool_name") if steps else "none"
    action_desc = (
        steps[0].get("description")
        if steps and steps[0].get("description")
        else f"Remediation executed on {target_resource}"
    )

    rca_dict = result.get("rca_result") or {}
    from nexus.engine.nodes.escalate import derive_escalation_reason

    resolved_reason = (
        result.get("verification_feedback") or "Resolved successfully"
        if is_resolved
        else derive_escalation_reason(result)
    )

    payload = {
        "incident_id": inc_id,
        "thread_id": thread_id,
        "app": target_ns,
        "target": target_resource,
        "target_name": target_name,
        "namespace": target_ns,
        "outcome": "success" if is_resolved else "failed",
        "failure_class": result.get("failure_class") or rca_dict.get("failure_type"),
        "suspected_cause": result.get("suspected_cause")
        or rca_dict.get("root_cause_summary"),
        "recommended_strategy": rca_dict.get("recommended_strategy")
        or plan_dict.get("rationale"),
        "evidence": rca_dict.get("evidence") or [],
        "rollback_applied": bool(result.get("rollback_results")),
        "rca": rca_dict,
        "plan": plan_dict,
        "verification": {
            "passed": result.get("verification_passed"),
            "feedback": result.get("verification_feedback"),
        },
        "execution_results": execution_results,
        "action_taken": action_desc,
        "confidence": float(
            result.get("confidence") or rca_dict.get("confidence") or 0.0
        ),
        "reason": resolved_reason,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }

    # 1. Learning plane recording
    runbook_id = result.get("failure_class") or action_type or "dynamic_remediation"
    try:
        from nexus.services.api.status_api import context

        if getattr(context, "knowledge_base", None) is not None:
            await context.knowledge_base.record_incident_outcome(
                incident_id=inc_id,
                runbook_id=str(runbook_id),
                action_type=str(action_type),
                resolved=is_resolved,
                reason=str(payload["reason"]),
            )
    except Exception as learn_exc:
        logger.warning(
            "[IncidentWorkflow] Learning recording failed for %s: %s", inc_id, learn_exc
        )

    # 2. Broadcast lifecycle event via NATS
    if nats and hasattr(nats, "publish_raw"):
        subject = (
            "nexus.lifecycle.resolved" if is_resolved else "nexus.lifecycle.escalated"
        )
        try:
            await nats.publish_raw(subject, payload)
        except Exception as e_pub:
            logger.warning(
                "[IncidentWorkflow] Failed to publish %s: %s", subject, e_pub
            )

    # 3. Persist to dashboard
    try:
        from nexus.services.observability.dashboard import record_incident_resolution

        await record_incident_resolution(payload)
    except Exception as e_dash:
        logger.debug("[IncidentWorkflow] Dashboard record skipped: %s", e_dash)

    # 4. Strictly guarded Post-Incident Report
    if publish_report:
        await finalize_workflow_run(inc_id, result)
