"""
NEXUS Status API Slack Interactive Callback Handler
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from nexus.integration import notifier as notifier_mod
from nexus.observability.status_context import _require, context

logger = logging.getLogger(__name__)
router = APIRouter(tags=["governance"])


def _slack_replace(
    text: str,
    *,
    status_title: str | None = None,
    is_approved: bool | None = None,
    approval_id: str | None = None,
    user: str | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {"replace_original": True, "text": text}
    if status_title or is_approved is not None:
        emoji = "✅" if is_approved else "❌"
        title = (
            status_title
            or f"{emoji} NEXUS Action {'Approved' if is_approved else 'Rejected'}"
        )
        color = "#10B981" if is_approved else "#EF4444"
        fields = []
        if approval_id:
            fields.append(
                {"type": "mrkdwn", "text": f"*Approval ID:*\n`{approval_id}`"}
            )
        if user:
            fields.append({"type": "mrkdwn", "text": f"*Operator:*\n@{user}"})
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*Decision:*\n`{'APPROVED' if is_approved else 'REJECTED'}`",
            }
        )

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title[:150],
                    "emoji": True,
                },
            },
            {"type": "divider"},
            {"type": "section", "fields": fields[:10]},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Status Update:*\n{text}"},
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "🛡️ *NEXUS Governance Audit* • Decision recorded • Action buttons deactivated",
                    }
                ],
            },
        ]
        response["blocks"] = blocks
        response["attachments"] = [{"color": color, "fallback": text, "blocks": blocks}]
    return response


async def _slack_replace_via_response_url(
    response_url: str | None,
    text: str,
    *,
    status_title: str | None = None,
    is_approved: bool | None = None,
    approval_id: str | None = None,
    user: str | None = None,
) -> None:
    if not response_url:
        return
    try:
        import httpx

        payload = _slack_replace(
            text,
            status_title=status_title,
            is_approved=is_approved,
            approval_id=approval_id,
            user=user,
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(response_url, json=payload)
    except Exception as exc:
        logger.warning(f"Failed to push delayed outcome to Slack response_url: {exc}")


async def _async_workflow_resume(
    workflow: Any,
    audit_trail: Any,
    approval_id: str,
    decision: str,
    user: str,
    response_url: str | None = None,
) -> None:
    try:
        if audit_trail:
            if decision == "approved":
                await audit_trail.record_approval(approval_id, f"slack:{user}")
            else:
                await audit_trail.record_rejection(approval_id, f"slack:{user}")
    except Exception as exc:
        logger.warning(f"Failed to record audit trail in background: {exc}")

    outcome = None
    try:
        outcome = await workflow.resume_incident(approval_id, decision)
    except Exception as exc:
        logger.error(f"Workflow resume failed for {approval_id}: {exc}")

    if response_url:
        is_ok = decision == "approved" and (
            outcome.get("resolved") if isinstance(outcome, dict) else True
        )
        msg = (
            f"Remediation `{approval_id}` executed successfully (authorized by @{user})."
            if is_ok
            else f"Remediation `{approval_id}` processed with decision '{decision}' (by @{user})."
        )
        await _slack_replace_via_response_url(
            response_url=response_url,
            text=msg,
            status_title=f"{'✅' if is_ok else 'ℹ️'} Remediation {'Completed' if is_ok else 'Finished'} — {approval_id}",
            is_approved=is_ok,
            approval_id=approval_id,
            user=user,
        )


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
        is_ok = outcome.get("status") in ("success", "executed", "ok")
        title = f"{'✅' if is_ok else '⚠️'} Execution Result: {outcome.get('status', 'complete')} — {approval_id}"
    except Exception as exc:
        logger.error(f"Slack approve dispatch failed: {exc}")
        text = f"Approved `{approval_id}` by @{user} but dispatch errored: {exc}"
        title = f"❌ Execution Failed — {approval_id}"
        is_ok = False
    await _slack_replace_via_response_url(
        response_url,
        text,
        status_title=title,
        is_approved=is_ok,
        approval_id=approval_id,
        user=user,
    )


@router.post("/slack/interactive")
@router.post("/api/v1/slack/interactive")
@router.post("/api/v1/slack/interactions")
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
    raw_value = action.get("value")
    user = data.get("user", {}).get("username") or data.get("user", {}).get(
        "id", "unknown"
    )
    user_id = data.get("user", {}).get("id") or user
    response_url = data.get("response_url")

    valid_actions = (
        "nexus_approve",
        "nexus_reject",
        "approve_plan",
        "reject_plan",
    )
    if not raw_value or action_id not in valid_actions:
        raise HTTPException(status_code=400, detail="malformed action")

    is_approval = action_id in ("nexus_approve", "approve_plan")

    # Check for structured JSON value (Plane 4)
    action_data = None
    if isinstance(raw_value, str) and raw_value.strip().startswith("{"):
        try:
            action_data = json.loads(raw_value)
        except Exception:
            action_data = None

    if isinstance(action_data, dict):
        incident_id = action_data.get("incident_id")
        plan_hash = action_data.get("plan_hash")
        expires_at = action_data.get("expires_at")

        if expires_at is not None:
            try:
                if time.time() > float(expires_at):
                    return _slack_replace(
                        "⏳ Approval request has expired. Incident requires re-planning.",
                        status_title="⏳ Approval Expired",
                        is_approved=False,
                    )
            except Exception:
                pass

        if incident_id:
            from nexus.bus.nats.client import NATSClient

            nats_client = NATSClient.get_instance()
            resume_payload = {
                "incident_id": incident_id,
                "plan_hash": plan_hash,
                "approved_by": user_id,
                "user": user,
                "decision": "approved" if is_approval else "rejected",
                "timestamp": time.time(),
            }
            if nats_client and getattr(nats_client, "is_connected", False):
                background_tasks.add_task(
                    nats_client.publish,
                    f"nexus.workflow.resume.{incident_id}",
                    resume_payload,
                )

            if is_approval:
                return {
                    "replace_original": True,
                    "text": "⚡ Approval verified. Resuming workflow...",
                }
            else:
                return {
                    "replace_original": True,
                    "text": "❌ Remediation plan rejected.",
                }

    approval_id = raw_value

    if (
        context.workflow
        and hasattr(context.workflow, "resume_incident")
        and hasattr(context.workflow, "has_pending")
        and context.workflow.has_pending(approval_id)
    ):
        if action_id == "nexus_approve":
            background_tasks.add_task(
                _async_workflow_resume,
                context.workflow,
                context.audit_trail,
                approval_id,
                "approved",
                user,
                response_url,
            )
            return _slack_replace(
                f"Approved `{approval_id}` by @{user} — executing remediation…",
                status_title=f"✅ Remediation Approved — {approval_id}",
                is_approved=True,
                approval_id=approval_id,
                user=user,
            )
        elif action_id == "nexus_reject":
            background_tasks.add_task(
                _async_workflow_resume,
                context.workflow,
                context.audit_trail,
                approval_id,
                "rejected",
                user,
                response_url,
            )
            return _slack_replace(
                f"Rejected `{approval_id}` by @{user}.",
                status_title=f"❌ Remediation Rejected — {approval_id}",
                is_approved=False,
                approval_id=approval_id,
                user=user,
            )

    orc = _require(context.orchestrator, "NexusOrchestrator")
    try:
        queue = orc.executor.ladder.approval_queue
    except AttributeError:
        raise HTTPException(status_code=503, detail="queue unavailable") from None

    if action_id == "nexus_approve":
        if queue.is_approved(approval_id):
            return _slack_replace(
                f"Action `{approval_id}` was already approved.",
                status_title=f"ℹ️ Action Already Approved — {approval_id}",
                is_approved=True,
                approval_id=approval_id,
                user=user,
            )
        pending_item = next(
            (p for p in queue.pending_list() if p.approval_id == approval_id), None
        )
        if not queue.approve(approval_id):
            return _slack_replace(f"Action `{approval_id}` not found.")
        if context.audit_trail:
            await context.audit_trail.record_approval(approval_id, f"slack:{user}")
        if pending_item is None:
            return _slack_replace(
                f"Approved `{approval_id}` by @{user} (staged snapshot lost — queued only).",
                status_title=f"⚠️ Action Approved (Queued Only) — {approval_id}",
                is_approved=True,
                approval_id=approval_id,
                user=user,
            )
        background_tasks.add_task(
            _slack_dispatch_approved,
            orc,
            pending_item,
            approval_id,
            user,
            response_url,
        )
        return _slack_replace(
            f"Approved `{approval_id}` by @{user} — executing…",
            status_title=f"✅ Remediation Approved — {approval_id}",
            is_approved=True,
            approval_id=approval_id,
            user=user,
        )

    if queue.is_rejected(approval_id):
        return _slack_replace(
            f"Action `{approval_id}` was already rejected.",
            status_title=f"ℹ️ Action Already Rejected — {approval_id}",
            is_approved=False,
            approval_id=approval_id,
            user=user,
        )
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
    return _slack_replace(
        f"Rejected `{approval_id}` by @{user}.",
        status_title=f"❌ Remediation Rejected — {approval_id}",
        is_approved=False,
        approval_id=approval_id,
        user=user,
    )
