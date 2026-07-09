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


def send_log_error_alert(
    function_name: str,
    log_group: str,
    error_lines: list[str],
    analysis: dict[str, Any],
    webhook_url: str = "",
) -> bool:
    """
    Send a Slack alert with raw error log lines + LLM's analysis.
    This is the primary notification function for the log monitor pipeline.

    Format:
      🔴 ERROR DETECTED in <function_name>
      ─────────────────────────────────
      Severity: High
      Root Cause: IAM permission missing — dynamodb:PutItem not allowed
      What it means: Writes to DynamoDB are failing silently
      How to fix:
        1. Add dynamodb:PutItem to the Lambda execution role
        2. Re-apply Terraform: terraform apply
      ─────────────────────────────────
      Raw log lines (last 5):
        [ERROR] AccessDeniedException: ...
    """
    url = webhook_url or _SLACK_WEBHOOK_URL
    if not url:
        logger.warning("[Slack] SLACK_WEBHOOK_URL not set — log error alert not sent")
        return False

    severity       = analysis.get("severity", "Unknown")
    root_cause     = analysis.get("root_cause", "Unable to determine")
    what_it_means  = analysis.get("what_it_means", "")
    how_to_fix     = analysis.get("how_to_fix", "Review the logs manually.")
    is_infra_issue = analysis.get("is_infra_issue", False)
    error_types    = analysis.get("error_types", [])

    # Color by severity
    color_map = {
        "Critical": "#8B0000",
        "High":     "#FF0000",
        "Medium":   "#FFA500",
        "Low":      "#FFFF00",
        "Unknown":  "#808080",
    }
    color = color_map.get(severity, "#808080")

    severity_emoji = {
        "Critical": "💀", "High": "🔴",
        "Medium": "🟡", "Low": "🟢", "Unknown": "⚪",
    }.get(severity, "⚪")

    infra_tag = " 🏗️ *Infrastructure Issue*" if is_infra_issue else ""

    # Trim log lines for the Slack message
    shown_lines = error_lines[-5:]
    log_block = "\n".join(f"  {line[:200]}" for line in shown_lines)

    fields = [
        {"title": "Function",   "value": f"`{function_name}`",  "short": True},
        {"title": "Log Group",  "value": f"`{log_group}`",       "short": True},
        {"title": "Severity",   "value": f"{severity_emoji} {severity}{infra_tag}", "short": True},
        {"title": "Error Count", "value": str(len(error_lines)), "short": True},
        {"title": "Root Cause", "value": root_cause,             "short": False},
        {"title": "What It Means", "value": what_it_means,       "short": False},
        {"title": "How To Fix",    "value": how_to_fix,           "short": False},
    ]

    if error_types:
        fields.append({"title": "Error Types", "value": ", ".join(f"`{e}`" for e in error_types), "short": False})

    payload = {
        "attachments": [
            {
                "color":   color,
                "title":   f"{severity_emoji} ERROR DETECTED in `{function_name}`",
                "pretext": "NEXUS AI DevOps Agent detected errors in CloudWatch Logs",
                "fields":  fields,
                "footer":  "NEXUS Log Monitor | CloudWatch Logs Subscription",
                "ts":      _now_ts(),
            },
            {
                "color":    "#333333",
                "title":    f"Raw Log Lines (last {len(shown_lines)} of {len(error_lines)})",
                "text":     f"```{log_block}```",
                "mrkdwn_in": ["text"],
            },
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
