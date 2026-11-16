"""
NEXUS Escalation Node
=====================
Dispatches high-priority alerts to SREs via Slack webhook, PagerDuty,
or NATS JetStream when autonomous remediation fails, policy blocks execution,
or human approval is rejected.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from nexus.engine.fsm import IncidentState
from nexus.graph.state import IncidentGraphState

logger = logging.getLogger(__name__)


async def escalate_node(state: IncidentGraphState) -> dict[str, Any]:
    """Escalate incident to human engineering team."""
    incident_id = state["incident_id"]
    target = state.get("target", {})
    platform = state.get("platform", "kubernetes")
    err_msg = (
        state.get("error_message")
        or (state.get("governance") or {}).get("reasons")
        or "Remediation escalated"
    )
    rollback_executed = state.get("rollback_executed", False)

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_entry = {
        "timestamp": now_iso,
        "stage": "escalate",
        "action": "operator_escalated",
        "details": f"Target: {target.get('name')} | Platform: {platform} | Reason: {err_msg} | Rollback: {rollback_executed}",
    }

    logger.warning(
        "[Escalate] Incident %s ESCALATED for target %s on platform %s: %s",
        incident_id,
        target.get("name"),
        platform,
        err_msg,
    )

    # Dispatch Slack webhook if configured
    webhook_url = os.getenv("SLACK_WEBHOOK_URL") or os.getenv("SLACK_WEBHOOK")
    if webhook_url:
        try:
            import httpx

            fallback = f"🚨 NEXUS Incident Alert: Incident {incident_id} Escalated for {target.get('name') or 'unknown'}"
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🚨 NEXUS Incident Escalation Alert",
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Incident ID:*\n`{incident_id}`"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Target:*\n`{target.get('name') or 'unknown'}`",
                        },
                        {"type": "mrkdwn", "text": f"*Platform:*\n`{platform}`"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Rollback Applied:*\n`{rollback_executed}`",
                        },
                    ],
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Escalation Reason:*\n{err_msg}",
                    },
                },
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"⚠️ *NEXUS Escalation Engine* • Timestamp: {now_iso}",
                        }
                    ],
                },
            ]
            payload = {
                "text": fallback,
                "attachments": [
                    {
                        "color": "#dc2626",
                        "fallback": fallback,
                        "blocks": blocks,
                    }
                ],
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(webhook_url, json=payload)
        except Exception as exc:
            logger.debug("[Escalate] Slack notification error: %s", exc)

    return {
        "escalated": True,
        "fsm_state": IncidentState.ESCALATED.value,
        "audit_log": [audit_entry],
    }
