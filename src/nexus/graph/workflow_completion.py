"""
Workflow completion handler for recording outcomes, publishing lifecycle events, and postmortems.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState

logger = logging.getLogger(__name__)


async def handle_workflow_completion(
    result: dict[str, Any],
    inc_id: str,
    thread_id: str,
    nats: Any = None,
) -> None:
    """Publish incident terminal events to NATS, persist to dashboard and learning plane."""
    fsm_state = result.get("fsm_state")
    is_resolved = bool(
        result.get("resolved") or fsm_state == IncidentState.RESOLVED.value
    )
    is_escalated = bool(
        result.get("escalated") or fsm_state == IncidentState.ESCALATED.value
    )

    if not (is_resolved or is_escalated):
        return

    target = result.get("target") or {}
    target_dict = (
        target.model_dump()
        if hasattr(target, "model_dump")
        else (target if isinstance(target, dict) else {})
    )
    target_ns = target_dict.get("namespace", "default")
    target_name = target_dict.get("name", "unknown")

    events = result.get("events") or []
    if not target_name or target_name == "unknown":
        if events and isinstance(events[0], dict):
            target_name = events[0].get("resource_name", target_name)
            target_ns = events[0].get("namespace", target_ns)

    signal_type = "unknown"
    if events and isinstance(events[0], dict):
        signal_type = events[0].get("signal_type") or "unknown"

    target_str = f"{target_ns}/{target_name}"

    diagnosis = result.get("diagnosis") or {}
    diag_dict = (
        diagnosis.model_dump()
        if hasattr(diagnosis, "model_dump")
        else (diagnosis if isinstance(diagnosis, dict) else {})
    )

    plan = result.get("plan") or {}
    plan_dict = (
        plan.model_dump()
        if hasattr(plan, "model_dump")
        else (plan if isinstance(plan, dict) else {})
    )

    verification = result.get("verification") or {}
    verif_dict = (
        verification.model_dump()
        if hasattr(verification, "model_dump")
        else (verification if isinstance(verification, dict) else {})
    )

    confidence = float(diag_dict.get("confidence") or 1.0)
    steps = plan_dict.get("steps", [])
    action_taken_desc = (
        steps[0].get("description")
        if steps and steps[0].get("description")
        else f"Remediation executed on {target_str}"
    )

    outcome_str = "success" if is_resolved else "failed"
    payload = {
        "incident_id": inc_id,
        "thread_id": thread_id,
        "app": target_ns,
        "target": target_str,
        "target_name": target_name,
        "namespace": target_ns,
        "outcome": outcome_str,
        "signal_type": signal_type,
        "rca": diag_dict,
        "plan": plan_dict,
        "verification": verif_dict,
        "action_taken": action_taken_desc,
        "confidence": confidence,
        "reason": result.get("error_message")
        or ("Resolved successfully" if is_resolved else "Escalated"),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }

    # 0. Learning plane recording
    runbook_id = plan_dict.get("failure_mode") or "autonomous_remediation"
    action_type = (
        steps[0].get("tool_name") if steps and isinstance(steps[0], dict) else None
    ) or "none"
    audit_outcome = (
        "success"
        if is_resolved
        else ("rolled_back" if result.get("rollback_executed") else "failed")
    )
    try:
        from nexus.observability.status_api import context

        if context.audit_trail is not None:
            await context.audit_trail.write(
                triggered_by="langgraph",
                runbook_id=runbook_id,
                healing_level=3,
                target=target_str,
                execution_outcome=audit_outcome,
                post_check_results=verif_dict or None,
                rollback_triggered=bool(result.get("rollback_executed")),
                incident_id=inc_id,
                action_results=result.get("execution_records") or [],
            )
        if context.knowledge_base is not None:
            await context.knowledge_base.record_incident_outcome(
                incident_id=inc_id,
                runbook_id=runbook_id,
                action_type=action_type,
                resolved=is_resolved,
                reason=payload["reason"],
            )
            if signal_type != "unknown":
                await context.knowledge_base.record_pattern(
                    signal_types={signal_type},
                    runbook_id=runbook_id,
                    success=is_resolved,
                )
        if context.audit_trail is not None or context.knowledge_base is not None:
            logger.info(
                "[IncidentWorkflow] Recorded learning outcome for %s (%s)",
                inc_id,
                audit_outcome,
            )
    except Exception as learn_exc:
        logger.warning(
            "[IncidentWorkflow] Learning-plane recording failed for %s: %s",
            inc_id,
            learn_exc,
        )

    # 1. Broadcast event via NATS
    if nats and hasattr(nats, "publish_raw"):
        subject = (
            "nexus.lifecycle.resolved" if is_resolved else "nexus.lifecycle.escalated"
        )
        try:
            await nats.publish_raw(subject, payload)
            logger.info(
                f"[IncidentWorkflow] Published {subject} for incident {inc_id} ({target_str})"
            )
        except Exception as e_pub:
            logger.warning(f"[IncidentWorkflow] Failed to publish {subject}: {e_pub}")

    # 2. Persist to PostgreSQL developer_incidents
    try:
        from nexus.integration.dashboard import record_incident_resolution

        await record_incident_resolution(payload)
        logger.info(
            f"[IncidentWorkflow] Recorded incident {inc_id} ({outcome_str}) to developer_incidents"
        )
    except Exception as e_dash:
        logger.warning(
            f"[IncidentWorkflow] Failed to record incident {inc_id} to dashboard store: {e_dash}"
        )

    # 3. Post-incident report
    try:
        from nexus.integration.incident_report import (
            generate_post_incident_report,
            report_url,
        )

        report = await generate_post_incident_report(result, payload=payload)
        sections = report.get("report") or {}
        summary = (sections.get("summary") or payload.get("action_taken") or "")[:700]
        if nats and hasattr(nats, "publish_raw"):
            await nats.publish_raw(
                "nexus.lifecycle.report_ready",
                {
                    "incident_id": inc_id,
                    "app": target_ns,
                    "target": target_str,
                    "outcome": payload.get("outcome"),
                    "report_url": report_url(inc_id),
                    "summary": summary,
                },
            )
            logger.info(
                f"[IncidentWorkflow] Published nexus.lifecycle.report_ready for {inc_id}"
            )
    except Exception as e_report:
        logger.warning(
            f"[IncidentWorkflow] Post-incident report failed for {inc_id}: {e_report}"
        )
