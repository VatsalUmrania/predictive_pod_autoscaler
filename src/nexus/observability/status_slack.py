"""
NEXUS Status API Slack Interactive Callback Handler
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from nexus.integration import notifier as notifier_mod
from nexus.observability.status_context import _require, context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["governance"])


def _slack_replace(text: str) -> dict[str, Any]:
    return {"replace_original": True, "text": text}


async def _slack_replace_via_response_url(response_url: str | None, text: str) -> None:
    if not response_url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                response_url, json={"replace_original": True, "text": text}
            )
    except Exception as exc:
        logger.warning(f"Failed to push delayed outcome to Slack response_url: {exc}")


async def _slack_dispatch_approved(
    orc: Any,
    pending_item: Any,
    approval_id: str,
    user: str,
    response_url: str | None,
) -> None:
    try:
        outcome = await orc.executor.execute_approved(pending_item)
        text = (
            f"Approved `{approval_id}` by @{user} → {outcome['status']} "
            f"(audit {outcome.get('action_id')})"
        )
    except Exception as exc:
        logger.error(f"Slack approve dispatch failed: {exc}")
        text = f"Approved `{approval_id}` by @{user} but dispatch errored: {exc}"
    await _slack_replace_via_response_url(response_url, text)


@router.post("/slack/interactive")
async def slack_interactive(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Slack interactive callback handler (Approve / Reject button clicks)."""
    raw = await request.body()
    logger.info(f"Slack interactivity: received raw body length={len(raw)}")

    ts = request.headers.get("X-Slack-Request-Timestamp", "missing")
    sig = request.headers.get("X-Slack-Signature", "missing")
    logger.info(
        f"Slack interactivity: ts={ts}, sig_prefix={sig[:20] if sig != 'missing' else 'missing'}..."
    )

    verify_fn = getattr(notifier_mod, "verify_slack_request", None)
    if not verify_fn or not verify_fn(raw, request.headers):
        signing_secret = getattr(notifier_mod, "SLACK_SIGNING_SECRET", None)
        logger.warning("Slack interactivity: INVALID SIGNATURE - rejecting (401)")
        logger.warning(
            f"  Expected sig check: SLACK_SIGNING_SECRET={'set' if signing_secret else 'MISSING!'}"
        )
        raise HTTPException(status_code=401, detail="invalid_signature")

    parsed = parse_qs(raw.decode("utf-8")).get("payload")
    payload = parsed[0] if parsed else None
    if not payload:
        raise HTTPException(status_code=400, detail="missing payload")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid payload") from None

    actions = data.get("actions", [])
    if not actions:
        raise HTTPException(status_code=400, detail="no actions")

    action = actions[0]
    action_id = action.get("action_id")
    approval_id = action.get("value")
    user = data.get("user", {}).get("username") or data.get("user", {}).get(
        "id", "unknown"
    )
    response_url = data.get("response_url")

    if not approval_id or action_id not in ("nexus_approve", "nexus_reject"):
        raise HTTPException(status_code=400, detail="malformed action")

    if (
        context.workflow
        and hasattr(context.workflow, "resume_incident")
        and hasattr(context.workflow, "has_pending")
        and context.workflow.has_pending(approval_id)
    ):
        if action_id == "nexus_approve":
            background_tasks.add_task(
                context.workflow.resume_incident, approval_id, "approved"
            )
            if context.audit_trail:
                await context.audit_trail.record_approval(approval_id, f"slack:{user}")
            return _slack_replace(
                f"Approved `{approval_id}` by @{user} — executing remediation…"
            )
        elif action_id == "nexus_reject":
            background_tasks.add_task(
                context.workflow.resume_incident, approval_id, "rejected"
            )
            if context.audit_trail:
                await context.audit_trail.record_rejection(approval_id, f"slack:{user}")
            return _slack_replace(f"Rejected `{approval_id}` by @{user}.")

    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.approval_queue
    except AttributeError:
        raise HTTPException(status_code=503, detail="queue unavailable") from None

    if action_id == "nexus_approve":
        if queue.is_approved(approval_id):
            return _slack_replace(f"Action `{approval_id}` was already approved.")
        pending_item = next(
            (p for p in queue.pending_list() if p.approval_id == approval_id), None
        )
        if not queue.approve(approval_id):
            return _slack_replace(f"Action `{approval_id}` not found.")
        if context.audit_trail:
            await context.audit_trail.record_approval(approval_id, f"slack:{user}")
        if pending_item is None:
            return _slack_replace(
                f"Approved `{approval_id}` by @{user} (staged snapshot lost — queued only)."
            )
        background_tasks.add_task(
            _slack_dispatch_approved,
            orc,
            pending_item,
            approval_id,
            user,
            response_url,
        )
        return _slack_replace(f"Approved `{approval_id}` by @{user} — executing…")

    if queue.is_rejected(approval_id):
        return _slack_replace(f"Action `{approval_id}` was already rejected.")
    pending_item = queue.get(approval_id)
    if not queue.reject(approval_id):
        return _slack_replace(f"Action `{approval_id}` not found.")
    if context.audit_trail:
        await context.audit_trail.record_rejection(approval_id, f"slack:{user}")
    if pending_item and orc and hasattr(orc, "notify_incident_rejected"):
        import asyncio

        res = orc.notify_incident_rejected(
            pending_item.target,
            pending_item.incident_id,
            reason=f"Rejected via Slack by @{user}",
        )
        if asyncio.iscoroutine(res):
            await res
    return _slack_replace(f"Rejected `{approval_id}` by @{user}.")
