"""
NEXUS Status API Governance and Approval Routes
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from nexus.engine.workflow.workflow_approval import handle_incident_approval
from nexus.services.api.status_context import _require, context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["governance"])


class ApprovalDecisionRequest(BaseModel):
    """Optional JSON payload for inline dashboard / API approval decisions."""

    incident_id: str | None = None
    approved: bool | None = None
    approver: str = "dashboard-operator"
    reason: str = ""


_OPTIONAL_APPROVAL_BODY = Body(default=None)


async def _has_checkpoint(workflow: Any, thread_id: str) -> bool:
    compiled_graph = getattr(workflow, "_compiled_graph", None)
    if compiled_graph is None or not hasattr(compiled_graph, "aget_state"):
        return False
    try:
        snapshot = await compiled_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        return bool(snapshot and getattr(snapshot, "values", None))
    except Exception:
        return False


async def _dispatch_workflow_decision(
    action_id: str,
    *,
    approved: bool,
    approver: str,
    reason: str,
) -> tuple[bool, Any]:
    """Resume a paused LangGraph thread via resume_incident or handle_incident_approval."""
    workflow = context.workflow
    if workflow is None:
        return False, None

    has_pending = bool(
        hasattr(workflow, "has_pending") and workflow.has_pending(action_id)
    )
    has_cp = await _has_checkpoint(workflow, action_id)
    is_wf_target = has_pending or has_cp or not context.orchestrator

    if not is_wf_target:
        return False, None

    decision_payload = {
        "approved": approved,
        "approved_by": approver,
        "approval_reason": reason,
    }

    if hasattr(workflow, "resume_incident"):
        outcome = await workflow.resume_incident(
            action_id, approval_decision=decision_payload
        )
        return True, outcome

    compiled_graph = getattr(workflow, "_compiled_graph", None)
    if compiled_graph is not None:
        outcome = await handle_incident_approval(
            compiled_graph=compiled_graph,
            incident_id=action_id,
            approved=approved,
            approver=approver,
            reason=reason,
        )
        return True, outcome

    return False, None


async def _record_audit_decision(
    action_id: str, *, approved: bool, approver: str
) -> None:
    if not context.audit_trail:
        return
    try:
        if approved:
            await context.audit_trail.record_approval(action_id, approver)
        else:
            await context.audit_trail.record_rejection(action_id, approver)
    except Exception as exc:
        logger.warning(
            "[StatusAPI] Failed recording audit decision for %s: %s", action_id, exc
        )


@router.get("/audit/tail")
async def audit_tail(n: int = 20) -> list[dict[str, Any]]:
    """Return the N most recent audit trail records."""
    at = _require(context.audit_trail, "AuditTrail")
    if hasattr(at, "tail"):
        return cast(list[dict[str, Any]], await at.tail(n))
    return cast(list[dict[str, Any]], await at.query_recent(limit=n))


@router.get("/audit/incident/{incident_id}")
async def audit_by_incident(incident_id: str) -> list[dict[str, Any]]:
    """Return all audit records for a specific incident/correlation ID."""
    at = _require(context.audit_trail, "AuditTrail")
    return cast(list[dict[str, Any]], await at.query_by_incident(incident_id))


@router.post("/approve/{action_id}")
async def approve_action(
    action_id: str,
    payload: ApprovalDecisionRequest | None = _OPTIONAL_APPROVAL_BODY,
) -> dict[str, Any]:
    """Approve a pending human-review action in LangGraph workflow."""
    approver = payload.approver if payload and payload.approver else "api_user"
    reason = payload.reason if payload else ""

    if context.workflow:
        try:
            handled, outcome = await _dispatch_workflow_decision(
                action_id, approved=True, approver=approver, reason=reason
            )
            if handled:
                await _record_audit_decision(
                    action_id, approved=True, approver=approver
                )
                return {
                    "status": "approved",
                    "action_id": action_id,
                    "incident_id": action_id,
                    "approved_by": approver,
                    "reason": reason,
                    "outcome": outcome,
                }
        except Exception as exc:
            logger.warning(f"Workflow approve failed for {action_id}: {exc}")
            return {"status": "approved", "action_id": action_id, "error": str(exc)}

    orc = context.orchestrator
    if orc and hasattr(orc, "executor"):
        try:
            ladder = orc.executor.ladder
            queue = ladder.approval_queue
            if queue.is_approved(action_id):
                return {"status": "already_approved", "action_id": action_id}
            pending_item = queue.get(action_id)
            if queue.approve(action_id):
                await _record_audit_decision(
                    action_id, approved=True, approver=approver
                )
                if pending_item:
                    asyncio.create_task(ladder.dispatch_approved(pending_item))
                return {"status": "approved", "action_id": action_id}
        except Exception as orc_exc:
            logger.debug(f"[StatusAPI] Legacy orchestrator approve failed: {orc_exc}")

    return {"status": "approved", "action_id": action_id}


@router.post("/reject/{action_id}")
async def reject_action(
    action_id: str,
    payload: ApprovalDecisionRequest | None = _OPTIONAL_APPROVAL_BODY,
) -> dict[str, Any]:
    """Reject a pending human-review action."""
    approver = payload.approver if payload and payload.approver else "api_user"
    reason = payload.reason if payload else ""

    if context.workflow:
        try:
            handled, outcome = await _dispatch_workflow_decision(
                action_id, approved=False, approver=approver, reason=reason
            )
            if handled:
                await _record_audit_decision(
                    action_id, approved=False, approver=approver
                )
                return {
                    "status": "rejected",
                    "action_id": action_id,
                    "incident_id": action_id,
                    "approved_by": approver,
                    "reason": reason,
                    "outcome": outcome,
                }
        except Exception as wf_exc:
            logger.info(
                f"[StatusAPI] Workflow reject attempted for {action_id}: {wf_exc}"
            )

    orc = context.orchestrator
    if not orc:
        return {"status": "rejected", "action_id": action_id}
    try:
        ladder = orc.executor.ladder
        queue = ladder.approval_queue
        if queue.is_rejected(action_id):
            return {"status": "already_rejected", "action_id": action_id}
        pending_item = queue.get(action_id)
        if queue.reject(action_id):
            await _record_audit_decision(action_id, approved=False, approver=approver)
            if pending_item and hasattr(orc, "notify_incident_rejected"):
                res = orc.notify_incident_rejected(
                    pending_item.target,
                    pending_item.incident_id,
                    reason=reason or "Rejected via HTTP API",
                )
                if asyncio.iscoroutine(res):
                    await res
            return {"status": "rejected", "action_id": action_id}
        raise HTTPException(
            status_code=404, detail=f"Action {action_id!r} not found in approval queue"
        )
    except AttributeError:
        raise HTTPException(
            status_code=503, detail="HumanApprovalQueue not accessible"
        ) from None


async def _dashboard_incident_decision(
    incident_id: str,
    *,
    approved: bool,
    payload: ApprovalDecisionRequest | None,
) -> dict[str, Any]:
    """Resume a paused LangGraph incident thread from the Developer Dashboard."""
    approver = (
        payload.approver if payload and payload.approver else "dashboard-operator"
    )
    reason = (
        payload.reason
        if payload and payload.reason
        else (
            "Approved via NEXUS Developer Console"
            if approved
            else "Rejected via NEXUS Developer Console"
        )
    )

    if context.workflow is None:
        raise HTTPException(status_code=503, detail="LangGraph workflow not configured")

    try:
        handled, outcome = await _dispatch_workflow_decision(
            incident_id, approved=approved, approver=approver, reason=reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except Exception as exc:
        logger.exception(
            "[StatusAPI] Dashboard %s failed for %s: %s",
            "approve" if approved else "reject",
            incident_id,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from None

    if not handled:
        raise HTTPException(
            status_code=404,
            detail=f"No paused LangGraph checkpoint found for incident {incident_id!r}",
        )

    await _record_audit_decision(incident_id, approved=approved, approver=approver)
    return {
        "status": "approved" if approved else "rejected",
        "incident_id": incident_id,
        "approved": approved,
        "approved_by": approver,
        "reason": reason,
        "outcome": outcome,
    }


@router.post("/developer/incidents/{incident_id}/approve", tags=["developer"])
@router.post("/api/v1/governance/approvals/{incident_id}/approve")
async def approve_incident_from_dashboard(
    incident_id: str,
    payload: ApprovalDecisionRequest | None = _OPTIONAL_APPROVAL_BODY,
) -> dict[str, Any]:
    """Inline dashboard endpoint to approve and resume a LangGraph interrupt() thread."""
    return await _dashboard_incident_decision(
        incident_id, approved=True, payload=payload
    )


@router.post("/developer/incidents/{incident_id}/reject", tags=["developer"])
@router.post("/api/v1/governance/approvals/{incident_id}/reject")
async def reject_incident_from_dashboard(
    incident_id: str,
    payload: ApprovalDecisionRequest | None = _OPTIONAL_APPROVAL_BODY,
) -> dict[str, Any]:
    """Inline dashboard endpoint to reject and escalate a LangGraph interrupt() thread."""
    return await _dashboard_incident_decision(
        incident_id, approved=False, payload=payload
    )


@router.get("/approvals/pending")
async def pending_approvals() -> list[dict[str, Any]]:
    """List all actions currently waiting in HumanApprovalQueue or LangGraph workflow."""
    results: list[dict[str, Any]] = []

    if context.workflow and hasattr(context.workflow, "pending_approvals"):
        try:
            wf_pending = context.workflow.pending_approvals()
            if wf_pending:
                results.extend(wf_pending)
        except Exception as exc:
            logger.warning(
                f"[StatusAPI] Failed querying workflow pending approvals: {exc}"
            )

    if context.orchestrator:
        try:
            queue = context.orchestrator.executor.ladder.approval_queue
            results.extend([p.to_dict() for p in queue.pending_list()])
        except Exception:
            pass

    return results
