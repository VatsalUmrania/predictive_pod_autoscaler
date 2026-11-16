"""
Pending approval management and notification helpers for IncidentWorkflow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, cast

from langgraph.types import Command

from nexus.engine.fsm import IncidentState
from nexus.graph.manifest_formatter import extract_diagnostic_logs, plan_to_yaml_manifest

logger = logging.getLogger(__name__)


def _dump(obj: Any) -> dict[str, Any]:
    fn = getattr(obj, "model_dump", getattr(obj, "to_dict", None))
    res = fn() if callable(fn) else obj
    return dict(res) if isinstance(res, dict) else {}


def create_pending_approval_entry(
    state_or_result: dict[str, Any], inc_id: str, thread_id: str
) -> dict[str, Any]:
    """Format pending approval entry from state or result with YAML manifest and logs."""
    plan_dict = _dump(state_or_result.get("plan") or state_or_result.get("remediation_plan"))
    steps = plan_dict.get("steps") or plan_dict.get("actions") or []
    action_type = steps[0].get("tool_name", "k8s_remediation") if steps else "k8s_remediation"
    runbook_id = plan_dict.get("failure_mode") or action_type or "dynamic_remediation"

    diag_dict = _dump(state_or_result.get("diagnosis") or state_or_result.get("rca_report"))
    confidence = float(diag_dict.get("confidence", 0.8))

    target_dict = _dump(state_or_result.get("target"))
    target_ns = target_dict.get("namespace", "default")
    target_name = target_dict.get("name", "unknown")
    target_str = f"{target_ns}/{target_name}"

    gov_dict = _dump(state_or_result.get("governance"))
    reasons = gov_dict.get("reasons", ["L2/L3 mutating action requires operator authorization"])
    suggested_fix = (
        diag_dict.get("suggested_fix")
        or (steps[0].get("description") if steps else None)
        or f"Execute {action_type} on {target_str}"
    )

    rem_yaml = plan_to_yaml_manifest(plan_dict, target=target_str)
    diag_logs = extract_diagnostic_logs(state_or_result)

    context = {
        "namespace": target_ns,
        "platform": state_or_result.get("platform", "kubernetes"),
        "thread_id": thread_id,
        "target": target_dict,
        "rca": diag_dict,
        "plan": plan_dict,
        "suggested_fix": suggested_fix,
        "remediation_yaml": rem_yaml,
        "diagnostic_logs": diag_logs,
    }

    return {
        "approval_id": thread_id,
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
                "app": entry["context"].get("namespace", "default"),
                "remediation_yaml": entry.get("remediation_yaml"),
                "diagnostic_logs": entry.get("diagnostic_logs"),
                "context": entry["context"],
            },
        )
        logger.info("[IncidentWorkflow] Published nexus.approvals.required for %s", entry["approval_id"])
    except Exception as e_pub:
        logger.warning("[IncidentWorkflow] Failed publishing nexus.approvals.required: %s", e_pub)


async def notify_incident_rejected_helper(
    active_incidents: dict[str, dict[str, Any]], nats: Any, target_key: str, incident_id: str, reason: str = ""
) -> None:
    inc_data = active_incidents.pop(target_key, None)
    if inc_data and "fsm" in inc_data:
        fsm = inc_data["fsm"]
        if hasattr(fsm, "transition_to"):
            await fsm.transition_to(IncidentState.REJECTED, reason=reason)
        elif hasattr(fsm, "_current_state"):
            fsm._current_state = IncidentState.REJECTED
    if nats and hasattr(nats, "publish_raw"):
        await nats.publish_raw("nexus.lifecycle.rejected", {"incident_id": incident_id, "target": target_key, "reason": reason})


async def handle_slack_button_click(
    compiled_graph: Any, incident_id: str, approved: bool, approver_name: str = "slack_operator"
) -> dict[str, Any]:
    config = {"configurable": {"thread_id": incident_id}}
    result = await compiled_graph.ainvoke(Command(resume={"approved": approved, "approver": approver_name}), config=config)
    return cast(dict[str, Any], result)


async def resume_incident_execution(
    workflow: Any, thread_id: str, approval_decision: str | dict[str, Any] = "approved"
) -> dict[str, Any]:
    entry = workflow._pending_approvals.get(thread_id)
    if not entry:
        for val in list(workflow._pending_approvals.values()):
            if val.get("approval_id") == thread_id or val.get("incident_id") == thread_id:
                entry = val
                break
    actual_tid = entry.get("thread_id", thread_id) if entry else thread_id
    if entry:
        for k in [entry.get("approval_id"), entry.get("thread_id"), entry.get("incident_id")]:
            if k:
                workflow._pending_approvals.pop(k, None)
    else:
        workflow._pending_approvals.pop(thread_id, None)

    config = {"configurable": {"thread_id": actual_tid}}
    logger.info("[IncidentWorkflow] Resuming incident %s with '%s'", actual_tid, approval_decision)
    result = await workflow._compiled_graph.ainvoke(Command(resume=approval_decision), config=config)

    state_snapshot = workflow._compiled_graph.get_state(config)
    if (state_snapshot.next and "approval" in state_snapshot.next) or result.get("__interrupt__"):
        await workflow._record_pending_approval(result, inc_id=result.get("incident_id", actual_tid), thread_id=actual_tid)
    else:
        dec_str = str(approval_decision.get("action") or approval_decision.get("decision") or "" if isinstance(approval_decision, dict) else approval_decision)
        if dec_str.lower() in ("rejected", "no", "false"):
            target_obj = result.get("target") or (entry.get("context", {}).get("target") if entry else {})
            t_str = target_obj.get("name", "unknown") if isinstance(target_obj, dict) else str(target_obj)
            await workflow.notify_incident_rejected(t_str, result.get("incident_id", actual_tid), reason=f"Rejected: {approval_decision}")
        await workflow._handle_workflow_completion(result, inc_id=result.get("incident_id", actual_tid), thread_id=actual_tid)

    if result.get("diagnosis"):
        workflow._rca_results.append(result["diagnosis"])
        workflow._rca_results = workflow._rca_results[-100:]
    return cast(dict[str, Any], result)
