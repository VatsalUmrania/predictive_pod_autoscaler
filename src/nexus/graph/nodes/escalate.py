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
    err_msg = state.get("error_message") or (state.get("governance") or {}).get("reasons") or "Remediation escalated"
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
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if webhook_url:
        try:
            import httpx

            payload = {
                "text": f"🚨 *[NEXUS Alert]* Incident `{incident_id}` Escalated\n"
                f"*Target:* `{target.get('name')}` ({platform})\n"
                f"*Reason:* {err_msg}\n"
                f"*Rollback Applied:* {rollback_executed}",
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
