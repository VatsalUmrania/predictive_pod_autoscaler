"""
NEXUS Status API Governance and Approval Routes
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from fastapi import APIRouter, HTTPException

from nexus.observability.status_context import _require, context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["governance"])


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
async def approve_action(action_id: str) -> dict[str, Any]:
    """Approve a pending human-review action in LangGraph workflow."""
    if context.workflow and hasattr(context.workflow, "resume_incident"):
        is_wf_target = (
            hasattr(context.workflow, "has_pending")
            and context.workflow.has_pending(action_id)
        ) or not context.orchestrator

        if is_wf_target:
            try:
                outcome = await context.workflow.resume_incident(
                    action_id, approval_decision="approved"
                )
                if context.audit_trail:
                    await context.audit_trail.record_approval(action_id, "api_user")
                return {"status": "approved", "action_id": action_id, "outcome": outcome}
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
                if context.audit_trail:
                    await context.audit_trail.record_approval(action_id, "api_user")
                if pending_item:
                    asyncio.create_task(ladder.dispatch_approved(pending_item))
                return {"status": "approved", "action_id": action_id}
        except Exception as orc_exc:
            logger.debug(f"[StatusAPI] Legacy orchestrator approve failed: {orc_exc}")

    return {"status": "approved", "action_id": action_id}


@router.post("/reject/{action_id}")
async def reject_action(action_id: str) -> dict[str, str]:
    """Reject a pending human-review action."""
    if context.workflow and hasattr(context.workflow, "resume_incident"):
        is_wf_target = (
            hasattr(context.workflow, "has_pending")
            and context.workflow.has_pending(action_id)
        ) or not context.orchestrator

        if is_wf_target:
            try:
                await context.workflow.resume_incident(
                    action_id, approval_decision="rejected"
                )
                if context.audit_trail:
                    await context.audit_trail.record_rejection(action_id, "api_user")
                return {"status": "rejected", "action_id": action_id}
            except Exception as wf_exc:
                logger.info(f"[StatusAPI] Workflow reject attempted for {action_id}: {wf_exc}")

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
            if context.audit_trail:
                await context.audit_trail.record_rejection(action_id, "api_user")
            if pending_item and hasattr(orc, "notify_incident_rejected"):
                res = orc.notify_incident_rejected(
                    pending_item.target,
                    pending_item.incident_id,
                    reason="Rejected via HTTP API",
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
            logger.warning(f"[StatusAPI] Failed querying workflow pending approvals: {exc}")

    if context.orchestrator:
        try:
            queue = context.orchestrator.executor.ladder.approval_queue
            results.extend([p.to_dict() for p in queue.pending_list()])
        except Exception:
            pass

    return results
