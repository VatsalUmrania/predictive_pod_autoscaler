"""
Human-in-the-loop approval resume and notification helpers for NEXUS workflows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast

from langgraph.types import Command

from nexus.engine.workflow.lifecycle.workflow_completion import finalize_workflow_run
from nexus.engine.workflow.state.manifest_formatter import (
    extract_diagnostic_logs,
    plan_to_yaml_manifest,
)
from nexus.engine.workflow.state.workflow_state import (
    sync_discovered_target_identity,
)

logger = logging.getLogger(__name__)


async def handle_incident_approval(
    compiled_graph: Any,
    incident_id: str,
    approved: bool,
    approver: str = "slack-operator",
    reason: str = "",
    *,
    notify_on_pause: bool = True,
) -> dict[str, Any]:
    """
    Resumes a paused LangGraph incident thread when an operator clicks Approve or Reject.
    Uses Command(resume=resume_payload) directly when snapshot.tasks has a pending
    LangGraph interrupt; falls back to aupdate_state + ainvoke(None) otherwise.
    """
    config = {"configurable": {"thread_id": incident_id}}
    snapshot = await compiled_graph.aget_state(config)

    if not snapshot or not snapshot.values:
        raise ValueError(f"No persisted checkpoint found for incident_id={incident_id}")

    resume_payload = {
        "approved": approved,
        "approval_status": "APPROVED" if approved else "REJECTED",
        "approved_by": approver,
        "approval_reason": reason,
    }

    has_pending_interrupt = any(
        getattr(task, "interrupts", None)
        for task in (getattr(snapshot, "tasks", None) or ())
    )

    logger.info(
        "Resuming incident %s from checkpoint (paused at %s, has_interrupt=%s) with approved=%s by %s",
        incident_id,
        snapshot.next,
        has_pending_interrupt,
        approved,
        approver,
    )

    if has_pending_interrupt:
        final_state = await compiled_graph.ainvoke(
            Command(resume=resume_payload), config=config
        )
    else:
        await compiled_graph.aupdate_state(config, resume_payload)
        final_state = await compiled_graph.ainvoke(None, config=config)

    # Check if the graph completed or paused again (e.g., closed-loop retry awaiting approval)
    post_snapshot = await compiled_graph.aget_state(config)
    if post_snapshot and post_snapshot.next:
        await sync_discovered_target_identity(incident_id, post_snapshot.values)
        if notify_on_pause:
            from nexus.services.notifications.dispatcher.notifier import (
                send_approval_request_notification,
            )

            await send_approval_request_notification(incident_id, post_snapshot.values)
        return cast(dict[str, Any], post_snapshot.values)

    await finalize_workflow_run(incident_id, final_state)
    return cast(dict[str, Any], final_state)


def create_pending_approval_entry(
    state_or_result: dict[str, Any], inc_id: str, thread_id: str
) -> dict[str, Any]:
    """Format pending approval entry from discovered IncidentState with YAML manifest."""
    plan_dict = dict(state_or_result.get("remediation_plan") or {})
    steps = plan_dict.get("steps") or []
    action_type = steps[0].get("tool_name", "remediation") if steps else "remediation"
    failure_class = state_or_result.get("failure_class") or action_type

    rca_dict = dict(state_or_result.get("rca_result") or {})
    raw_conf = state_or_result.get("confidence", rca_dict.get("confidence", 0.0))
    confidence = float(raw_conf if raw_conf is not None else 0.0)

    target_ns = state_or_result.get("namespace")
    target_kind = state_or_result.get("resource_kind")
    target_name = state_or_result.get("service_name")
    target_str = state_or_result.get("target_resource") or (
        f"{target_ns}/{target_kind or 'Resource'}/{target_name}"
        if target_ns and target_name
        else (target_name or inc_id)
    )

    policy_dec = dict(state_or_result.get("policy_decision") or {})
    reasons = (
        [policy_dec["reason"]]
        if policy_dec.get("reason")
        else ["Mutating remediation requires operator authorization"]
    )
    suggested_fix = (
        rca_dict.get("recommended_strategy")
        or (steps[0].get("description") if steps else None)
        or f"Execute {action_type} on {target_str}"
    )

    rem_yaml = plan_to_yaml_manifest(plan_dict, target=target_str)
    diag_logs = extract_diagnostic_logs(state_or_result)

    context = {
        "namespace": target_ns,
        "resource_kind": target_kind,
        "service_name": target_name,
        "target_resource": target_str,
        "platform": state_or_result.get("platform"),
        "thread_id": thread_id,
        "failure_class": failure_class,
        "suspected_cause": state_or_result.get("suspected_cause"),
        "rca": rca_dict,
        "plan": plan_dict,
        "suggested_fix": suggested_fix,
        "remediation_yaml": rem_yaml,
        "diagnostic_logs": diag_logs,
    }

    return {
        "approval_id": thread_id,
        "thread_id": thread_id,
        "incident_id": inc_id,
        "runbook_id": failure_class,
        "action_type": action_type,
        "target": target_str,
        "healing_level": 3,
        "confidence": round(confidence, 3),
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "reasons": reasons,
        "prompt": suggested_fix,
        "suggested_fix": suggested_fix,
        "rca": rca_dict,
        "plan": plan_dict,
        "remediation_yaml": rem_yaml,
        "diagnostic_logs": diag_logs,
        "context": context,
    }


async def publish_pending_approval(nats: Any, entry: dict[str, Any]) -> None:
    """Publish pending approval notification over NATS with manifest and logs."""
    if not (nats and hasattr(nats, "publish_raw")):
        return
    try:
        await nats.publish_raw(
            "nexus.approvals.required",
            {
                "approval_id": entry["approval_id"],
                "runbook_id": entry["runbook_id"],
                "action_type": entry["action_type"],
                "target": entry["target"],
                "incident_id": entry["incident_id"],
                "healing_level": 3,
                "confidence": entry["confidence"],
                "app": entry["context"].get("namespace") or "unscoped",
                "remediation_yaml": entry.get("remediation_yaml"),
                "diagnostic_logs": entry.get("diagnostic_logs"),
                "context": entry["context"],
            },
        )
    except Exception as e_pub:
        logger.warning(
            "[IncidentWorkflow] Failed publishing nexus.approvals.required: %s", e_pub
        )


async def notify_incident_rejected_helper(
    active_incidents: dict[str, dict[str, Any]],
    nats: Any,
    target_key: str,
    incident_id: str,
    reason: str = "",
) -> None:
    active_incidents.pop(target_key, None)
    if nats and hasattr(nats, "publish_raw"):
        await nats.publish_raw(
            "nexus.lifecycle.rejected",
            {"incident_id": incident_id, "target": target_key, "reason": reason},
        )


async def handle_slack_button_click(
    compiled_graph: Any,
    incident_id: str,
    approved: bool,
    approver_name: str = "slack-operator",
) -> dict[str, Any]:
    return await handle_incident_approval(
        compiled_graph=compiled_graph,
        incident_id=incident_id,
        approved=approved,
        approver=approver_name,
    )


async def resume_incident_execution(
    workflow: Any,
    thread_id: str,
    approval_decision: str | dict[str, Any] = "approved",
) -> dict[str, Any]:
    """IncidentWorkflow bridge delegating to handle_incident_approval."""
    entry = workflow._pending_approvals.get(thread_id)
    if not entry:
        for val in list(workflow._pending_approvals.values()):
            if (
                val.get("approval_id") == thread_id
                or val.get("incident_id") == thread_id
            ):
                entry = val
                break
    actual_tid = entry.get("thread_id", thread_id) if entry else thread_id
    if entry:
        for k in (
            entry.get("approval_id"),
            entry.get("thread_id"),
            entry.get("incident_id"),
        ):
            if k:
                workflow._pending_approvals.pop(k, None)
    else:
        workflow._pending_approvals.pop(thread_id, None)

    if isinstance(approval_decision, dict):
        if "approved" in approval_decision:
            approved = bool(approval_decision["approved"])
        else:
            raw = str(
                approval_decision.get("action")
                or approval_decision.get("decision")
                or ""
            ).lower()
            approved = raw in ("approved", "yes", "true", "proceed")
        approver = str(approval_decision.get("approved_by") or "slack-operator")
        reason = str(approval_decision.get("approval_reason") or "")
    else:
        approved = str(approval_decision).lower() in (
            "approved",
            "yes",
            "true",
            "proceed",
        )
        approver = "slack-operator"
        reason = ""

    has_nats_publisher = bool(
        getattr(workflow, "nats", None) and hasattr(workflow.nats, "publish_raw")
    )
    result = await handle_incident_approval(
        compiled_graph=workflow._compiled_graph,
        incident_id=actual_tid,
        approved=approved,
        approver=approver,
        reason=reason,
        notify_on_pause=not has_nats_publisher,
    )

    config = {"configurable": {"thread_id": actual_tid}}
    post_snapshot = await workflow._compiled_graph.aget_state(config)
    if post_snapshot and post_snapshot.next:
        await workflow._record_pending_approval(
            post_snapshot.values,
            inc_id=post_snapshot.values.get("incident_id", actual_tid),
            thread_id=actual_tid,
        )
    else:
        if not approved:
            target_str = str(
                result.get("target_resource")
                or result.get("service_name")
                or actual_tid
            )
            await workflow.notify_incident_rejected(
                target_str,
                result.get("incident_id", actual_tid),
                reason=reason or f"Rejected by {approver}",
            )
        if hasattr(workflow, "_handle_workflow_completion"):
            await workflow._handle_workflow_completion(
                result,
                inc_id=result.get("incident_id", actual_tid),
                thread_id=actual_tid,
                publish_report=False,
            )

    if result.get("rca_result"):
        workflow._rca_results.append(result["rca_result"])
        workflow._rca_results = workflow._rca_results[-100:]

    return result
