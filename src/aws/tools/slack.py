"""
Slack Notification Tools
=========================
Sends messages to Slack via Incoming Webhooks.
Uses httpx for async-compatible HTTP (sync calls are fine in Lambda).

Set SLACK_WEBHOOK_URL in environment variables.
Get your webhook URL from: https://api.slack.com/messaging/webhooks
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


def send_alert(
    message: str,
    title: str = "NEXUS AI DevOps Agent",
    color: str = "#FF0000",    # Red for alerts
    fields: list[dict] | None = None,
    webhook_url: str = "",
) -> bool:
    """
    Send a formatted Slack alert.

    Args:
        message:     Main message text
        title:       Block title
        color:       Attachment sidebar color (hex)
        fields:      Optional list of {"title": ..., "value": ..., "short": bool}
        webhook_url: Override SLACK_WEBHOOK_URL env var

    Returns True if sent successfully, False on error.
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — alert not sent")
        return False

    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "text": message,
                "fields": fields or [],
                "footer": "NEXUS AI DevOps Agent",
                "ts": _now_ts(),
            }
        ]
    }

    return _post_to_slack(url, payload)


def send_incident_alert(
    resource_name: str,
    signal_type: str,
    root_cause: str,
    action_taken: str,
    confidence: float,
    resolved: bool,
    webhook_url: str = "",
) -> bool:
    """
    Send a structured incident notification to Slack.
    Used after remediation to report the outcome.
    """
    icon = "✅" if resolved else "🔴"
    color = "#36a64f" if resolved else "#FF0000"
    status = "RESOLVED" if resolved else "UNRESOLVED — Manual action required"

    fields = [
        {"title": "Resource", "value": f"`{resource_name}`", "short": True},
        {"title": "Signal", "value": signal_type, "short": True},
        {"title": "Root Cause", "value": root_cause, "short": False},
        {"title": "Action Taken", "value": action_taken, "short": True},
        {"title": "Confidence", "value": f"{confidence:.0%}", "short": True},
        {"title": "Status", "value": status, "short": False},
    ]

    return send_alert(
        message=f"{icon} *{status}*",
        title="NEXUS Incident Report",
        color=color,
        fields=fields,
        webhook_url=webhook_url,
    )


def send_approval_request(
    resource_name: str,
    action: str,
    params: dict[str, Any],
    root_cause: str,
    confidence: float,
    webhook_url: str = "",
) -> bool:
    """
    Send a Slack message requesting human approval for a low-confidence action.

    NOTE: Full approval/reject buttons require Slack App with interactive components.
    For now, this sends a clear alert asking for manual review.
    A future version can use Slack Block Kit actions with a callback URL.
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — approval request not sent")
        return False

    fields = [
        {"title": "Resource", "value": f"`{resource_name}`", "short": True},
        {"title": "Proposed Action", "value": action, "short": True},
        {"title": "Parameters", "value": f"```{json.dumps(params, indent=2)}```", "short": False},
        {"title": "Root Cause", "value": root_cause, "short": False},
        {"title": "Confidence", "value": f"{confidence:.0%} (below auto-execute threshold)", "short": False},
    ]

    payload = {
        "attachments": [
            {
                "color": "#FFA500",     # Orange for approval requests
                "title": "⚠️ NEXUS: Human Approval Required",
                "text": (
                    "NEXUS has diagnosed an incident but confidence is below the auto-execute threshold.\n"
                    "*Please review and take manual action if appropriate.*"
                ),
                "fields": fields,
                "footer": "NEXUS AI DevOps Agent | Auto-execute threshold: 85%",
                "ts": _now_ts(),
            }
        ]
    }

    return _post_to_slack(url, payload)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _post_to_slack(url: str, payload: dict) -> bool:
    """POST payload to Slack webhook URL. Returns True on 200."""
    try:
        import urllib.request
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status == 200
            if not ok:
                logger.warning(f"[Slack] Unexpected status {resp.status}")
            return ok
    except Exception as exc:
        logger.error(f"[Slack] Failed to send message: {exc}")
        return False


def _now_ts() -> int:
    from datetime import datetime, timezone
    return int(datetime.now(timezone.utc).timestamp())
