"""
Pending approval management and notification helpers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from langgraph.types import Command

from nexus.engine.fsm import IncidentState

logger = logging.getLogger(__name__)


def create_pending_approval_entry(
    state_or_result: dict[str, Any],
    inc_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Format pending approval entry from state or result."""
    plan = state_or_result.get("plan") or {}
    plan_dict = (
        plan.model_dump()
        if hasattr(plan, "model_dump")
        else (plan if isinstance(plan, dict) else {})
    )

    steps = plan_dict.get("steps", [])
    action_type = (
        steps[0].get("tool_name", "k8s_remediation") if steps else "k8s_remediation"
    )
    runbook_id = plan_dict.get("failure_mode") or "autonomous_remediation"
    confidence = float(state_or_result.get("diagnosis", {}).get("confidence", 0.8))

    target = state_or_result.get("target") or {}
    target_dict = (
        target.model_dump()
        if hasattr(target, "model_dump")
        else (target if isinstance(target, dict) else {})
    )
    target_ns = target_dict.get("namespace", "default")
    target_name = target_dict.get("name", "unknown")
    target_str = f"{target_ns}/{target_name}"

    gov = state_or_result.get("governance") or {}
    gov_dict = (
        gov.model_dump()
        if hasattr(gov, "model_dump")
        else (gov if isinstance(gov, dict) else {})
    )
    reasons = gov_dict.get(
        "reasons", ["L2/L3 mutating action requires operator authorization"]
    )

    diagnosis = state_or_result.get("diagnosis") or {}
    if hasattr(diagnosis, "model_dump"):
        diag_dict = diagnosis.model_dump()
    elif hasattr(diagnosis, "to_dict"):
        diag_dict = diagnosis.to_dict()
    elif isinstance(diagnosis, dict):
        diag_dict = diagnosis
    else:
        diag_dict = {}

    suggested_fix = (
        diag_dict.get("suggested_fix")
        or (steps[0].get("description") if steps else None)
        or f"Execute {action_type} on {target_str}"
    )

    approval_id = thread_id
    return {
        "approval_id": approval_id,
        "thread_id": thread_id,
        "incident_id": inc_id,
        "runbook_id": runbook_id,
        "action_type": action_type,
        "target": target_str,
        "healing_level": 3,
        "confidence": round(confidence, 3),
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "reasons": reasons,
        "prompt": suggested_fix,
        "suggested_fix": suggested_fix,
        "rca": diag_dict,
        "plan": plan_dict,
        "context": {
            "namespace": target_ns,
            "platform": state_or_result.get("platform", "kubernetes"),
            "thread_id": thread_id,
            "target": target_dict,
            "rca": diag_dict,
            "suggested_fix": suggested_fix,
        },
    }


async def publish_pending_approval(nats: Any, entry: dict[str, Any]) -> None:
    """Publish pending approval notification over NATS."""
    if nats and hasattr(nats, "publish_raw"):
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
                    "app": entry["context"].get("namespace", "default"),
                    "context": entry["context"],
                },
            )
            logger.info(
                "[IncidentWorkflow] Published nexus.approvals.required for %s",
                entry["approval_id"],
            )
        except Exception as e_pub:
            logger.warning(
                "[IncidentWorkflow] Failed publishing nexus.approvals.required: %s",
                e_pub,
            )


async def notify_incident_rejected_helper(
    active_incidents: dict[str, dict[str, Any]],
    nats: Any,
    target_key: str,
    incident_id: str,
    reason: str = "",
) -> None:
    """Clear active incident tracking and mark FSM as rejected."""
    inc_data = active_incidents.pop(target_key, None)
    if inc_data and "fsm" in inc_data:
        fsm = inc_data["fsm"]
        if hasattr(fsm, "transition_to"):
            await fsm.transition_to(IncidentState.REJECTED, reason=reason)
        elif hasattr(fsm, "_current_state"):
            fsm._current_state = IncidentState.REJECTED
    if nats and hasattr(nats, "publish_raw"):
        await nats.publish_raw(
            "nexus.lifecycle.rejected",
            {"incident_id": incident_id, "target": target_key, "reason": reason},
        )


async def resume_incident_execution(
    workflow: Any,
    thread_id: str,
    approval_decision: str = "approved",
) -> dict[str, Any]:
    """Resume an interrupted incident after human authorization."""
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
        for k in [
            entry.get("approval_id"),
            entry.get("thread_id"),
            entry.get("incident_id"),
        ]:
            if k and k in workflow._pending_approvals:
                workflow._pending_approvals.pop(k, None)
    else:
        workflow._pending_approvals.pop(thread_id, None)

    config = {"configurable": {"thread_id": actual_tid}}
    logger.info(
        "[IncidentWorkflow] Resuming incident on thread %s with '%s'",
        actual_tid,
        approval_decision,
    )

    result = await workflow._compiled_graph.ainvoke(
        Command(resume=approval_decision), config=config
    )

    state_snapshot = workflow._compiled_graph.get_state(config)
    if (state_snapshot.next and "approval" in state_snapshot.next) or result.get(
        "__interrupt__"
    ):
        inc_id = result.get("incident_id", actual_tid)
        await workflow._record_pending_approval(
            result, inc_id=inc_id, thread_id=actual_tid
        )
    else:
        dec_str = (
            approval_decision.get("action") or approval_decision.get("decision") or ""
            if isinstance(approval_decision, dict)
            else str(approval_decision)
        )
        if dec_str.lower() in ("rejected", "no", "false"):
            target_obj = result.get("target") or (
                entry.get("context", {}).get("target") if entry else {}
            )
            target_str = (
                target_obj.get("name", "unknown")
                if isinstance(target_obj, dict)
                else str(target_obj)
            )
            await workflow.notify_incident_rejected(
                target_str,
                result.get("incident_id", actual_tid),
                reason=f"Rejected by operator: {approval_decision}",
            )
        await workflow._handle_workflow_completion(
            result, inc_id=result.get("incident_id", actual_tid), thread_id=actual_tid
        )

    if result.get("diagnosis"):
        workflow._rca_results.append(result["diagnosis"])
        workflow._rca_results = workflow._rca_results[-100:]
    return result
